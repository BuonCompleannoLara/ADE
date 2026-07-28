"""
Ricava il chat_id del bot Telegram.

Uso:
  1. scrivi un messaggio qualsiasi al tuo bot su Telegram
  2. python3 get_chat_id.py
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

# stesso token di default di monitor_esame.py, il .env ha comunque la precedenza
DEFAULT_BOT_TOKEN = "8978289517:AAFJy0-42EP6rw8C9kRnsHNo9vcp-UrLdhQ"
token = os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN

resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)

if resp.status_code == 401:
    sys.exit("Token rifiutato da Telegram (401). Il token e' sbagliato o e' stato revocato.")
resp.raise_for_status()

data = resp.json()
if not data.get("ok"):
    sys.exit(f"Telegram ha risposto con un errore: {data}")

updates = data.get("result", [])
if not updates:
    sys.exit(
        "Nessun messaggio trovato.\n"
        "Apri Telegram, scrivi un messaggio qualsiasi al bot, poi rilancia questo script."
    )

chats = {}
for u in updates:
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat")
    if chat:
        chats[chat["id"]] = chat.get("username") or chat.get("first_name") or "?"

print("Chat trovate:\n")
for chat_id, name in chats.items():
    print(f"  chat_id: {chat_id}   ({name})")
print("\nCopia il numero in TELEGRAM_CHAT_ID nel file .env")
