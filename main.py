import os
import sys
from pathlib import Path

import anthropic
from twilio.rest import Client
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLAUDE_MD_PATH = Path(__file__).parent / "CLAUDE.md"
USERS_DIR = Path(__file__).parent / "users"

app = Flask(__name__)

conversation_histories: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_context(user_number: str = "") -> str:
    """Load per-user context from users/ folder, falling back to CLAUDE.md."""
    if user_number:
        clean_number = user_number.replace("whatsapp:", "")
        user_file = USERS_DIR / f"{clean_number}.md"
        if user_file.exists():
            return user_file.read_text(encoding="utf-8").strip()

    # Fallback for unknown numbers or legacy usage
    if CLAUDE_MD_PATH.exists():
        return CLAUDE_MD_PATH.read_text(encoding="utf-8").strip()

    print("Warning: No context file found. Proceeding without context.")
    return ""


def get_registered_numbers() -> list[str]:
    """Return a list of phone numbers that have a context file in users/."""
    if not USERS_DIR.exists():
        return []
    return [f.stem for f in USERS_DIR.glob("*.md")]


def generate_reply(sender: str, incoming_msg: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "Error: ANTHROPIC_API_KEY is not configured."

    client = anthropic.Anthropic(api_key=api_key)
    context = load_context(sender)

    system_prompt = (
        "You are MealBuddy, a helpful nutritionist and meal planning assistant. "
        "Answer questions about meals, nutrition, recipes, and the user's meal plan."
    )
    if context:
        system_prompt += f"\n\nContext about the user:\n{context}"

    history = conversation_histories.setdefault(sender, [])
    history.append({"role": "user", "content": incoming_msg})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system_prompt,
        messages=history,
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    return reply


def send_whatsapp(message: str, to_number: str = "") -> None:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not to_number:
        to_number = os.environ.get("MY_PHONE_NUMBER", "")

    if not all([account_sid, auth_token, to_number]):
        raise EnvironmentError("Missing Twilio credentials or phone number.")

    client = Client(account_sid, auth_token)
    client.messages.create(
        from_="whatsapp:+14155238886",
        body=message,
        to=f"whatsapp:{to_number}",
    )
    print(f"WhatsApp message sent to {to_number}!")


def saturday_kickoff() -> None:
    """Send the Saturday kickoff message to all registered users."""
    print("Running Saturday kickoff job...")

    # Build the list of numbers to message
    registered = get_registered_numbers()
    fallback_number = os.environ.get("MY_PHONE_NUMBER", "")

    # If no users/ folder yet, fall back to env var
    if not registered and fallback_number:
        registered = [fallback_number]
    elif not registered:
        print("Error: No registered users and MY_PHONE_NUMBER not set. Skipping.")
        return

    for phone_number in registered:
        sender_key = f"whatsapp:{phone_number}"

        # Load this user's context to personalize the greeting
        context = load_context(sender_key)

        # Extract a name from the context if available, otherwise use a generic greeting
        name = "there"
        if context:
            for line in context.splitlines():
                lower = line.lower()
                if "name:" in lower or "name is" in lower:
                    # Grab the value after "name:" or "name is"
                    for sep in ["name:", "name is"]:
                        if sep in lower:
                            name = line.split(sep, 1)[1].strip().split()[0]
                            break
                    break

        # Clear conversation history for a fresh week
        conversation_histories[sender_key] = []

        opening_msg = (
            f"Hey {name}! It's MealBuddy Saturday.\n\n"
            "Two quick questions to build your week:\n"
            "1. What cuisines or meal vibes are you feeling this week?\n"
            "2. What's your grocery budget?\n\n"
            "Reply and I'll put together your full 7-day plan + grocery list "
            "with store prices. Stay consistent."
        )

        try:
            send_whatsapp(opening_msg, phone_number)
            print(f"Saturday kickoff sent to {phone_number}!")
        except Exception as e:
            print(f"Error sending to {phone_number}: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    print(f"Incoming WhatsApp message from {sender}: {incoming_msg}")

    reply_text = generate_reply(sender, incoming_msg)

    resp = MessagingResponse()
    resp.message(reply_text)
    return str(resp)


@app.route("/health", methods=["GET"])
def health():
    return "MealBuddy webhook server is running.", 200


@app.route("/saturday", methods=["GET"])
def trigger_saturday():
    try:
        saturday_kickoff()
        return "Saturday kickoff triggered successfully!", 200
    except Exception as e:
        return f"Error: {e}", 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "send"

    if mode == "server":
        port = int(os.environ.get("PORT", 5000))
        eastern = pytz.timezone("America/New_York")

        scheduler = BackgroundScheduler(timezone=eastern)
        scheduler.add_job(
            saturday_kickoff,
            CronTrigger(day_of_week="sat", hour=19, minute=0, timezone=eastern),
        )
        scheduler.start()
        print("Saturday scheduler started -- fires every Saturday at 7:00 PM ET")

        # Log registered users on startup
        registered = get_registered_numbers()
        if registered:
            print(f"Registered users: {', '.join(registered)}")
        else:
            print("No users/ folder found — using CLAUDE.md as fallback context.")

        print(f"Starting MealBuddy WhatsApp webhook server on port {port}...")
        app.run(host="0.0.0.0", port=port, debug=False)

    else:
        try:
            saturday_kickoff()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
