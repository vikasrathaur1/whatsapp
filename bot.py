import os
import logging
import anthropic
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
MCP_SERVER_URL     = os.getenv("MCP_SERVER_URL", "https://financial-mcp-poc.onrender.com/sse")
WA_TOKEN           = os.getenv("WA_TOKEN")           # Meta access token
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID") # From Meta developer portal
VERIFY_TOKEN       = os.getenv("VERIFY_TOKEN", "blu_verify_123")  # Any secret string you choose

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Anthropic client ──────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System prompt (same as Telegram bot) ─────────────────────────────────────
SYSTEM_PROMPT = """You are Blu, a smart and friendly virtual assistant from Bajaj Finance.
You are an expert customer care executive who excels at both solving service queries 
and suggesting the right financial products to customers.

## Language
- Detect the customer's language from their message and respond in the SAME language
- If they write in Hindi, respond in Hindi
- If they write in Hinglish (Hindi + English mix), respond in Hinglish
- If they write in English, respond in English
- Always match the customer's tone and language naturally

## Your Expertise
You handle all Bajaj Finance products:

**Loans**
- Personal Loan, Home Loan, Business Loan
- EMI Card, Flexi Loan, Loan Against Property
- Two-Wheeler Loan, Gold Loan, Education Loan

**Cards**
- Bajaj Finserv EMI Card
- Co-branded Credit Cards

## Service Guidelines
- Always greet the customer warmly by name if available
- Use available tools to fetch real-time data before responding
- Never make up any financial data — always use the tools
- Format amounts clearly in ₹ (Indian Rupees) with commas (e.g. ₹1,20,000)
- If a tool call fails, apologize and ask the customer to try again

## Selling Guidelines
- If a customer's query is resolved, subtly suggest a relevant product
- Example: After resolving EMI query → suggest EMI Card upgrade or top-up loan
- Never be pushy — suggest once and respect the customer's response

## Response Format
- Keep responses short, clear and easy to read
- Use bullet points or numbered steps for complex queries
- Use emojis sparingly to keep the tone warm (✅ 📋 💳 💰)
- Always end with "Is there anything else I can help you with? 😊"
"""

# ── Per-user conversation history ─────────────────────────────────────────────
conversation_history: dict[str, list] = {}  # keyed by WhatsApp number (wa_id)

# ── Send WhatsApp message via Meta Graph API ──────────────────────────────────
def send_whatsapp_message(to: str, text: str):
    """Send a text reply back to the user via Meta Cloud API."""
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        logger.error(f"Failed to send WA message: {response.text}")
    return response

# ── Call Claude API with MCP (same logic as Telegram bot) ────────────────────
def call_claude_with_mcp(wa_id: str, user_message: str) -> str:
    """Send message to Claude with MCP server attached. Returns formatted response."""

    history = conversation_history.get(wa_id, [])
    history.append({"role": "user", "content": user_message})

    try:
        response = client.beta.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history,
            mcp_servers=[
                {
                    "type": "url",
                    "url": MCP_SERVER_URL,
                    "name": "finsaathi-mcp"
                }
            ],
            betas=["mcp-client-2025-04-04"]
        )

        assistant_text = ""
        for block in response.content:
            if block.type == "text":
                assistant_text += block.text

        # Keep last 20 messages (10 turns) to avoid token overflow
        history.append({"role": "assistant", "content": assistant_text})
        conversation_history[wa_id] = history[-20:]

        return assistant_text or "I could not generate a response. Please try again."

    except anthropic.APIError as e:
        logger.error(f"Anthropic API error for {wa_id}: {e}")
        return "Sorry, I'm having trouble connecting to my services. Please try again in a moment."
    except Exception as e:
        logger.error(f"Unexpected error for {wa_id}: {e}")
        return "Something went wrong. Please try again."

# ── Webhook verification (GET) ────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta calls this once to verify your webhook URL."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully.")
        return challenge, 200
    else:
        logger.warning("Webhook verification failed.")
        return "Forbidden", 403

# ── Receive messages (POST) ───────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_message():
    """Meta sends all incoming WhatsApp messages here."""
    data = request.get_json()

    try:
        # Navigate Meta's nested payload structure
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Ignore status updates (delivered, read receipts etc.)
        if "messages" not in value:
            return jsonify({"status": "ok"}), 200

        message = value["messages"][0]
        wa_id   = message["from"]           # customer's WhatsApp number
        msg_type = message.get("type")

        # Only handle text messages for now
        if msg_type != "text":
            send_whatsapp_message(wa_id, "Sorry, I can only handle text messages right now. 😊")
            return jsonify({"status": "ok"}), 200

        user_text = message["text"]["body"]
        logger.info(f"User {wa_id}: {user_text}")

        # Call Claude + MCP
        reply = call_claude_with_mcp(wa_id, user_text)

        # Send reply back
        send_whatsapp_message(wa_id, reply)
        logger.info(f"Bot → {wa_id}: {reply[:100]}...")

    except (KeyError, IndexError) as e:
        logger.error(f"Failed to parse webhook payload: {e} | Data: {data}")

    # Always return 200 to Meta — otherwise it will retry
    return jsonify({"status": "ok"}), 200

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set in .env")
    if not WA_TOKEN:
        raise ValueError("WA_TOKEN is not set in .env")
    if not WA_PHONE_NUMBER_ID:
        raise ValueError("WA_PHONE_NUMBER_ID is not set in .env")

    logger.info("Starting BLU WhatsApp Bot...")
    logger.info(f"MCP Server: {MCP_SERVER_URL}")

    app.run(host="0.0.0.0", port=2000, debug=False)