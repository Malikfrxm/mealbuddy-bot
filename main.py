import os
import sys
from pathlib import Path

import anthropic
from twilio.rest import Client
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

CLAUDE_MD_PATH = Path(__file__).parent / "CLAUDE.md"

app = Flask(__name__)

conversation_histories: dict[str, list[dict]] = {}


def load_context() -> str:
    if CLAUDE_MD_PATH.exists():
        return CLAUDE_MD_PATH.read_text(encoding="utf-8").strip()
    print("Warning: CLAUDE.md not found. Proceeding without context.")
    return ""


def generate_reply(sender: str, incoming_msg: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "Error: ANTHROPIC_API_KEY is not configured."
    client = anthropic.Anthropic(api_key=api_key)
    context = load_context()
    system_prompt = (
        "You are MealBuddy, a helpful nutritionist and meal planning assistant. "
        "Answer questions about meals, nutrition, recipes, and the user's meal plan."
    )
    if context:
        system_prompt += f"\n\nContext about the user:\n{context}"
    history = conversation_histories.setdefault(sender, [])
    history.append({"role": "user", "content": incoming_msg})
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        system=system_prompt,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply


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


def generate_meal_plan() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not configured.")
    client = anthropic.Anthropic(api_key=api_key)
    context = load_context()
    system_prompt = (
        "You are a helpful nutritionist and meal planning assistant. "
        "Create detailed, practical weekly meal plans tailored to the user."
    )
    if context:
        system_prompt += f"\n\nContext about the user:\n{context}"
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": "Please create a detailed weekly meal plan for me, including breakfast, lunch, dinner, and snacks for each day."}],
    )
    return message.content[0].text


def send_whatsapp(message: str) -> None:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    my_phone = os.environ.get("MY_PHONE_NUMBER")
    if not all([account_sid, auth_token, my_phone]):
        raise EnvironmentError("Missing Twilio credentials or MY_PHONE_NUMBER.")
    client = Client(account_sid, auth_token)
    client.messages.create(
        from_="whatsapp:+14155238886",
        body=message,
        to=f"whatsapp:{my_phone}",
    )
    print("Meal plan sent successfully via WhatsApp!")


def main() -> None:
    print("Generating weekly meal plan...")
    meal_plan = generate_meal_plan()
    print("Sending meal plan via WhatsApp...")
    send_whatsapp(meal_plan)
    print("Done.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "send"
    if mode == "server":
        port = int(os.environ.get("PORT", 5000))
        print(f"Starting MealBuddy WhatsApp webhook server on port {port}...")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        try:
            main()
        except EnvironmentError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)
            sys.exit(1)
