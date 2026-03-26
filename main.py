import os
import sys
import tempfile
from pathlib import Path

import anthropic
import openai
import requests as http_requests
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

# Audio MIME types Twilio may deliver for WhatsApp voice memos
AUDIO_MIME_TYPES = {
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/3gpp": "3gp",
    "audio/amr": "amr",
}


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

    # Fallback for unknown numbers
    if CLAUDE_MD_PATH.exists():
        return CLAUDE_MD_PATH.read_text(encoding="utf-8").strip()

    print("Warning: No context file found. Proceeding without context.")
    return ""


def get_registered_numbers() -> list[str]:
    """Return list of phone numbers that have a profile file in users/."""
    if not USERS_DIR.exists():
        return []
    return [f.stem for f in USERS_DIR.glob("*.md")]


def transcribe_audio(media_url: str) -> str:
    """Download a Twilio audio attachment and transcribe it with OpenAI Whisper."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("Warning: OPENAI_API_KEY not set — cannot transcribe audio.")
        return "[Audio message received, but transcription is not configured]"

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

    # Twilio protects media URLs — authenticate with account credentials
    try:
        audio_resp = http_requests.get(
            media_url, auth=(account_sid, auth_token), timeout=30
        )
        audio_resp.raise_for_status()
    except Exception as exc:
        print(f"Error downloading audio from Twilio: {exc}")
        return "[Could not retrieve audio message]"

    # Determine file extension from Content-Type (strip codec suffix if present)
    content_type = audio_resp.headers.get("Content-Type", "audio/ogg")
    base_type = content_type.split(";")[0].strip()
    ext = AUDIO_MIME_TYPES.get(base_type, "ogg")

    # Write to a temp file so the OpenAI SDK can stream it
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(audio_resp.content)
        tmp_path = Path(tmp.name)

    try:
        client_oai = openai.OpenAI(api_key=openai_key)
        with open(tmp_path, "rb") as audio_file:
            transcript = client_oai.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        print(f"Whisper transcription: {transcript.text!r}")
        return transcript.text
    except Exception as exc:
        print(f"Whisper transcription error: {exc}")
        return "[Audio transcription failed]"
    finally:
        tmp_path.unlink(missing_ok=True)


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
    """Send the Saturday 7PM kickoff to every registered user."""
    print("Running Saturday kickoff job...")

    registered = get_registered_numbers()
    fallback_number = os.environ.get("MY_PHONE_NUMBER", "")

    all_numbers = set(registered)
    if fallback_number:
        all_numbers.add(fallback_number)

    if not all_numbers:
        print("No registered users and MY_PHONE_NUMBER not set — skipping.")
        return

    for number in all_numbers:
        sender_key = f"whatsapp:{number}"
        context = load_context(sender_key)

        # Extract first name from the profile if possible
        name = "there"
        if context:
            for line in context.splitlines():
                lower = line.lower()
                if "name:" in lower or "name is" in lower:
                    for sep in ["name:", "name is"]:
                        if sep in lower:
                            name = line.split(sep, 1)[1].strip().split()[0]
                            name = name.strip("*_#")
                            break
                    break

        # Reset conversation history for a fresh weekly plan
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
            send_whatsapp(opening_msg, number)
            print(f"Saturday kickoff sent to {number}")
        except Exception as exc:
            print(f"Error sending kickoff to {number}: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
    num_media = int(request.form.get("NumMedia", "0"))

    print(f"Message from {sender} | text={incoming_msg!r} | media={num_media}")

    # --- Voice memo / audio MMS handling ---
    if num_media > 0:
        media_url = request.form.get("MediaUrl0", "")
        media_type = request.form.get("MediaContentType0", "")
        base_type = media_type.split(";")[0].strip()

        if media_url and base_type.startswith("audio/"):
            print(f"Audio attachment ({media_type}) — sending to Whisper...")
            incoming_msg = transcribe_audio(media_url)
        elif not incoming_msg:
            # Non-audio media with no text — acknowledge and bail
            resp = MessagingResponse()
            resp.message("(I received a media file but can only process audio and text.)")
            return str(resp)

    if not incoming_msg:
        return str(MessagingResponse())

    reply_text = generate_reply(sender, incoming_msg)
    resp = MessagingResponse()
    resp.message(reply_text)
    return str(resp)


@app.route("/health", methods=["GET"])
def health():
    registered = get_registered_numbers()
    return f"MealBuddy running. Registered users: {registered}", 200


@app.route("/saturday", methods=["GET"])
def trigger_saturday():
    try:
        saturday_kickoff()
        return "Saturday kickoff triggered successfully!", 200
    except Exception as exc:
        return f"Error: {exc}", 500


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

        registered = get_registered_numbers()
        if registered:
            print(f"Registered users: {', '.join(registered)}")
        else:
            print("No users registered yet (add .md files to users/ directory).")

        print(f"Starting MealBuddy WhatsApp webhook server on port {port}...")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        try:
            saturday_kickoff()
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
