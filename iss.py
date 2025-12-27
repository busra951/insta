#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Gerekli paketler:
#   pip install pyTelegramBotAPI instagrapi requests

import os
import re
import time
import json
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional
import threading

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, PleaseWaitFewMinutes, RateLimitError, ChallengeRequired,
    TwoFactorRequired, UserNotFound, ClientError
)

# ========= AYARLAR =========
TOKEN = "7933146275:AAFxCF7XcVhgkRsWXuN78PGazw-yqdveNWs"
BOT_NAME = "İçerik Avcısı 🤖"
FETCH_WINDOW_MIN = 60
CHECK_INTERVAL_MIN = 5  # Her 5 dakikada bir kontrol et

# Instagram bilgileri (login olan hesap)
IG_USER = os.getenv("IG_USER") or "asafcagiller"
IG_PASS = os.getenv("IG_PASS") or "asaf2727*"
SESSION_FILE = f"ig_session_{IG_USER}.json"

# Kanal veritabanı
CHANNELS_FILE = "channels_data.json"

# Telegram bot
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# Instagram client
cl = Client()

# ========= VERİTABANI FONKSİYONLARI =========
def save_channels(data):
    """Kanal verilerini kaydet"""
    with open(CHANNELS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_channels():
    """Kanal verilerini yükle + eski yapıyı yeniye migrate et"""
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except:
            return {}

        changed = False
        # Eski yapı: ig_username + last_post_id -> Yeni yapı: ig_accounts dict
        for ch_id, ch in data.items():
            if 'ig_accounts' not in ch:
                ig_username = ch.get('ig_username')
                last_post_id = ch.get('last_post_id')
                ch['ig_accounts'] = {}
                if ig_username:
                    ch['ig_accounts'][ig_username] = {'last_post_id': last_post_id}
                ch.pop('ig_username', None)
                ch.pop('last_post_id', None)
                changed = True
        if changed:
            save_channels(data)

        return data
    return {}

def get_channel_info(channel_id):
    """Kanal bilgilerini al"""
    try:
        chat = bot.get_chat(channel_id)
        return {
            'id': channel_id,
            'title': chat.title,
            'username': f"@{chat.username}" if chat.username else "Gizli Kanal"
        }
    except Exception:
        return None

# ========= YARDIMCI FONKSİYONLAR =========
def safe_send_message(chat_id, text, **kwargs):
    """Güvenli mesaj gönder"""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except ApiTelegramException as e:
        if "parse" in str(e).lower():
            kwargs.pop('parse_mode', None)
            return bot.send_message(chat_id, text, **kwargs)
        raise

def within_last_minutes(dt: datetime, minutes: int) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - dt) <= timedelta(minutes=minutes)

# ========= INSTAGRAM FONKSİYONLARI =========
USERNAME_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?", re.IGNORECASE)

def extract_ig_username(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    m = USERNAME_RE.match(text)
    if m:
        return m.group(1)
    if text.startswith("@"):
        text = text[1:]
    return text.split("/")[0]

def ig_load_session() -> bool:
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.set_locale("tr_TR")
            cl.delay_range = [1, 3]
            cl.request_timeout = 30
            cl.get_timeline_feed()
            return True
        except Exception:
            pass
    return False

def ig_login():
    if not IG_USER or IG_USER in {"ig_kullanici_adin", "kullanici_adin", "GERCEK_IG_KULLANICI_ADIN"}:
        raise RuntimeError("IG_USER ayarlı değil. Gerçek IG kullanıcı adını ver.")
    if not IG_PASS or IG_PASS in {"ig_parolan", "parola", "GERCEK_IG_SIFREN"}:
        raise RuntimeError("IG_PASS ayarlı değil. Gerçek IG şifresini ver.")

    cl.set_locale("tr_TR")
    cl.delay_range = [1, 3]
    cl.request_timeout = 30

    if ig_load_session():
        return

    try:
        cl.login(IG_USER, IG_PASS)
        cl.dump_settings(SESSION_FILE)
    except TwoFactorRequired:
        code = input("Instagram 2FA (SMS/Auth) kodu: ").strip()
        cl.two_factor_login(code)
        cl.dump_settings(SESSION_FILE)
    except ChallengeRequired:
        raise RuntimeError("Instagram 'ChallengeRequired' verdi. Uygulamadan giriş yapıp tekrar dene.")
    except Exception as e:
        raise RuntimeError(f"Instagram login başarısız: {type(e).__name__} {e}")

# Pydantic fallback için
try:
    from pydantic_core import ValidationError as PydanticValidationError
except ImportError:
    try:
        from pydantic import ValidationError as PydanticValidationError
    except ImportError:
        PydanticValidationError = Exception

def ig_get_recent_medias(username: str):
    try:
        user_info = cl.user_info_by_username_v1(username)
    except UserNotFound:
        raise RuntimeError("Profil bulunamadı.")
    except (PleaseWaitFewMinutes, RateLimitError):
        raise RuntimeError("Instagram limit verdi. Biraz sonra tekrar dene.")
    except LoginRequired:
        cl.relogin()
        cl.dump_settings(SESSION_FILE)
        user_info = cl.user_info_by_username_v1(username)
    except Exception as e:
        raise RuntimeError(f"Kullanıcı bilgisi alınamadı: {type(e).__name__} {e}")

    if getattr(user_info, "is_private", False):
        raise RuntimeError(f"@{username} profili gizli.")

    user_id = user_info.pk

    try:
        medias = cl.user_medias_v1(user_id, amount=12)
    except PydanticValidationError as e:
        print(f"[WARN] Pydantic validation hatası (user_medias_v1): {e}")
        try:
            medias = cl.user_medias(user_id, amount=12)
        except Exception as e2:
            raise RuntimeError(f"Gönderiler alınamadı (fallback): {type(e2).__name__} {e2}")
    except (PleaseWaitFewMinutes, RateLimitError):
        raise RuntimeError("Instagram limit verdi.")
    except LoginRequired:
        cl.relogin()
        cl.dump_settings(SESSION_FILE)
        medias = cl.user_medias_v1(user_id, amount=12)
    except Exception as e:
        raise RuntimeError(f"Gönderiler alınamadı: {type(e).__name__} {e}")

    recent = [m for m in medias if within_last_minutes(m.taken_at, FETCH_WINDOW_MIN)]
    return recent

def media_to_items(m) -> List[Tuple[str, str]]:
    items = []
    try:
        if m.media_type == 1:
            url = getattr(m, "thumbnail_url", None) or getattr(m, "url", None)
            if url:
                items.append(("photo", url))
        elif m.media_type == 2:
            vurl = getattr(m, "video_url", None)
            if not vurl:
                try:
                    info = cl.media_info(m.pk)
                    vurl = getattr(info, "video_url", None)
                except Exception:
                    vurl = None
            if vurl:
                items.append(("video", vurl))
        elif m.media_type == 8:
            for r in m.resources:
                if getattr(r, "video_url", None):
                    items.append(("video", r.video_url))
                else:
                    url = getattr(r, "thumbnail_url", None) or getattr(r, "url", None)
                    if url:
                        items.append(("photo", url))
    except Exception:
        pass

    seen, uniq = set(), []
    for t, u in items:
        if u and u not in seen:
            uniq.append((t, u))
            seen.add(u)
    return uniq

def format_ig_caption(m, label: str) -> str:
    """
    Önceden: 📸 @username • tarih
    Şimdi:   📸 Kanal Adı  (veya @username), zaman YOK
    """
    base = f"<b>📸 {label}</b>"
    cap = getattr(m, "caption_text", "") or ""
    if len(cap) > 900:
        cap = cap[:900] + "…"
    return base + ("\n" + cap if cap else "")

def send_media_to_channel(channel_id: int, media: List[Tuple[str, str]], caption: str):
    """Kanala medya gönder"""
    if not media:
        safe_send_message(channel_id, caption)
        return

    if len(media) > 1:
        group = []
        for idx, (t, url) in enumerate(media[:10]):
            if t == "video":
                group.append(types.InputMediaVideo(url, caption=caption if idx == 0 else None))
            else:
                group.append(types.InputMediaPhoto(url, caption=caption if idx == 0 else None))
        try:
            bot.send_media_group(channel_id, group)
        except Exception:
            for idx, (t, url) in enumerate(media[:10]):
                try:
                    if t == "video":
                        bot.send_video(channel_id, url, caption=caption if idx == 0 else None)
                    else:
                        bot.send_photo(channel_id, url, caption=caption if idx == 0 else None)
                except:
                    pass
    else:
        t, url = media[0]
        try:
            if t == "video":
                bot.send_video(channel_id, url, caption=caption)
            else:
                bot.send_photo(channel_id, url, caption=caption)
        except Exception:
            safe_send_message(channel_id, caption)

# ========= TELEGRAM KOMUTLARI =========
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    text = (
        f"Merhaba! {BOT_NAME}'e hoş geldin 👋\n\n"
        "<b>📋 Kanal Yönetimi:</b>\n"
        "• <b>/ekle</b> - Yeni kanal ekle\n"
        "• <b>/liste</b> - Eklenen kanalları göster\n"
        "• <b>/sec</b> - Kanal seç ve Instagram kaynak profillerini yönet\n"
        "• <b>/sil</b> - Kanal sil\n\n"
        "<b>📸 Manuel İçerik Çekme:</b>\n"
        "• <b>/ig</b> - Instagram profili ara\n\n"
        f"Bot her <b>{CHECK_INTERVAL_MIN} dakika</b>da bir eklenen kanalları kontrol eder "
        f"ve son <b>{FETCH_WINDOW_MIN} dakika</b> içindeki gönderileri gönderir."
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=["ekle"])
def cmd_add_channel(message: types.Message):
    msg = bot.reply_to(
        message,
        "📢 Eklemek istediğin kanalın ID'sini gönder.\n\n"
        "<b>Kanal ID nasıl bulunur?</b>\n"
        "1. Botu kanala admin yap\n"
        "2. /getid komutunu kanala gönder\n"
        "3. ID'yi buraya yapıştır (örn: <code>-1001234567890</code>)"
    )
    bot.register_next_step_handler(msg, handle_add_channel)

def handle_add_channel(message: types.Message):
    try:
        channel_id = message.text.strip()

        if not channel_id.startswith("-100"):
            bot.reply_to(message, "❌ Geçersiz kanal ID! -100 ile başlamalı.")
            return

        channel_id = int(channel_id)

        info = get_channel_info(channel_id)
        if not info:
            bot.reply_to(message, "❌ Kanal bulunamadı! Botu kanala admin olarak ekledin mi?")
            return

        channels = load_channels()
        channels[str(channel_id)] = {
            'title': info['title'],
            'username': info['username'],
            'ig_accounts': {}  # username -> {"last_post_id": ...}
        }
        save_channels(channels)

        bot.reply_to(
            message,
            f"✅ Kanal eklendi!\n\n"
            f"<b>📢 {info['title']}</b>\n"
            f"ID: <code>{channel_id}</code>\n"
            f"{info['username']}\n\n"
            f"Şimdi <b>/sec</b> komutu ile bu kanala Instagram profilleri ekleyebilirsin."
        )

    except ValueError:
        bot.reply_to(message, "❌ Geçersiz ID formatı!")
    except Exception as e:
        bot.reply_to(message, f"❌ Hata: {type(e).__name__}")

@bot.message_handler(commands=["liste"])
def cmd_list_channels(message: types.Message):
    channels = load_channels()

    if not channels:
        bot.reply_to(message, "📋 Henüz kanal eklenmemiş.\n\n<b>/ekle</b> komutu ile kanal ekleyebilirsin.")
        return

    text = "<b>📋 Eklenen Kanallar:</b>\n\n"

    for idx, (ch_id, data) in enumerate(channels.items(), 1):
        ig_accounts = data.get('ig_accounts') or {}
        if ig_accounts:
            ig_list = ", ".join(f"@{u}" for u in ig_accounts.keys())
        else:
            ig_list = "❌ Belirlenmemiş"

        text += (
            f"{idx}. <b>{data['title']}</b>\n"
            f"   ID: <code>{ch_id}</code>\n"
            f"   IG Hesapları: <code>{ig_list}</code>\n\n"
        )

    bot.reply_to(message, text)

@bot.message_handler(commands=["sec"])
def cmd_select_channel(message: types.Message):
    channels = load_channels()

    if not channels:
        bot.reply_to(message, "📋 Henüz kanal eklenmemiş.\n\n<b>/ekle</b> komutu ile kanal ekle.")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)

    for ch_id, data in channels.items():
        ig_accounts = data.get('ig_accounts') or {}
        ig_status = f"{len(ig_accounts)} IG hesabı" if ig_accounts else "IG yok"
        btn = types.InlineKeyboardButton(
            f"{data['title']} - {ig_status}",
            callback_data=f"select_{ch_id}"
        )
        markup.add(btn)

    bot.reply_to(message, "<b>📢 Bir kanal seç:</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_"))
def callback_select_channel(call):
    channel_id = call.data.replace("select_", "")
    channels = load_channels()
    ch = channels.get(channel_id)
    if not ch:
        bot.answer_callback_query(call.id, "Kanal bulunamadı")
        return

    ig_accounts = ch.get('ig_accounts') or {}
    if ig_accounts:
        ig_text = "\n".join(f"• @{u}" for u in ig_accounts.keys())
    else:
        ig_text = "Henüz IG hesabı eklenmemiş."

    text = (
        f"<b>📢 {ch['title']}</b>\n"
        f"ID: <code>{channel_id}</code>\n\n"
        f"<b>📸 IG Hesapları:</b>\n{ig_text}\n\n"
        "Aşağıdan işlem seç:"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("➕ IG Hesabı Ekle", callback_data=f"addig_{channel_id}"),
        types.InlineKeyboardButton("🗑 IG Hesabı Sil", callback_data=f"delig_{channel_id}")
    )

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("addig_"))
def callback_add_ig(call):
    channel_id = call.data.replace("addig_", "")
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "📸 <b>Yeni IG kullanıcı adını gönder:</b>\n\n"
        "Örnek: <code>bpthaber</code> veya <code>@bpthaber</code>",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, handle_add_ig_username, channel_id)

def handle_add_ig_username(message: types.Message, channel_id: str):
    ig_username = extract_ig_username(message.text or "")
    if not ig_username:
        bot.reply_to(message, "❌ Geçersiz kullanıcı adı!")
        return

    channels = load_channels()
    ch = channels.get(channel_id)
    if not ch:
        bot.reply_to(message, "❌ Kanal bulunamadı!")
        return

    if 'ig_accounts' not in ch or not isinstance(ch['ig_accounts'], dict):
        ch['ig_accounts'] = {}

    if ig_username in ch['ig_accounts']:
        bot.reply_to(message, f"ℹ️ @{ig_username} zaten bu kanala eklenmiş.")
    else:
        ch['ig_accounts'][ig_username] = {'last_post_id': None}
        save_channels(channels)
        bot.reply_to(
            message,
            f"✅ @{ig_username} bu kanala eklendi!\n\n"
            f"📢 Kanal: <b>{ch['title']}</b>"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("delig_"))
def callback_del_ig_menu(call):
    channel_id = call.data.replace("delig_", "")
    channels = load_channels()
    ch = channels.get(channel_id)
    if not ch:
        bot.answer_callback_query(call.id, "Kanal bulunamadı")
        return

    ig_accounts = ch.get('ig_accounts') or {}
    if not ig_accounts:
        bot.answer_callback_query(call.id, "Bu kanalda IG hesabı yok")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for u in ig_accounts.keys():
        markup.add(
            types.InlineKeyboardButton(
                f"🗑 @{u}",
                callback_data=f"deligdo_{channel_id}_{u}"
            )
        )
    markup.add(
        types.InlineKeyboardButton("❌ İptal", callback_data="delig_cancel")
    )

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "<b>🗑 Silmek istediğin IG hesabını seç:</b>",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("deligdo_") or call.data == "delig_cancel")
def callback_del_ig_do(call):
    if call.data == "delig_cancel":
        bot.answer_callback_query(call.id, "İptal edildi")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    _, channel_id, username = call.data.split("_", 2)
    channels = load_channels()
    ch = channels.get(channel_id)
    if not ch:
        bot.answer_callback_query(call.id, "Kanal bulunamadı")
        return

    ig_accounts = ch.get('ig_accounts') or {}
    if username in ig_accounts:
        del ig_accounts[username]
        save_channels(channels)
        bot.answer_callback_query(call.id, f"@{username} silindi")
        bot.edit_message_text(
            f"✅ @{username} bu kanaldan silindi.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    else:
        bot.answer_callback_query(call.id, "Hesap bulunamadı")

@bot.message_handler(commands=["sil"])
def cmd_delete_channel(message: types.Message):
    channels = load_channels()

    if not channels:
        bot.reply_to(message, "📋 Silinecek kanal yok.")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)

    for ch_id, data in channels.items():
        btn = types.InlineKeyboardButton(
            f"🗑️ {data['title']}",
            callback_data=f"delete_{ch_id}"
        )
        markup.add(btn)

    markup.add(types.InlineKeyboardButton("❌ İptal", callback_data="delete_cancel"))

    bot.reply_to(message, "<b>🗑️ Silmek istediğin kanalı seç:</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
def callback_delete_channel(call):
    if call.data == "delete_cancel":
        bot.answer_callback_query(call.id, "İptal edildi")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    channel_id = call.data.replace("delete_", "")

    channels = load_channels()
    if channel_id in channels:
        channel_title = channels[channel_id]['title']
        del channels[channel_id]
        save_channels(channels)

        bot.answer_callback_query(call.id, "Kanal silindi")
        bot.edit_message_text(
            f"✅ <b>{channel_title}</b> silindi.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    else:
        bot.answer_callback_query(call.id, "Kanal bulunamadı")

@bot.message_handler(commands=["getid"])
def cmd_get_id(message: types.Message):
    """Kanal veya kullanıcı ID'sini öğren"""
    chat_id = message.chat.id
    chat_type = message.chat.type

    if chat_type in ["group", "supergroup", "channel"]:
        bot.reply_to(message, f"📢 Bu kanalın/grubun ID'si: <code>{chat_id}</code>")
    else:
        bot.reply_to(message, f"👤 Senin ID'n: <code>{chat_id}</code>")

# ========= MANUEL İÇERİK ÇEKME =========
@bot.message_handler(commands=["instagram", "ig"])
def cmd_instagram(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        username = extract_ig_username(parts[1])
        if username:
            wait = bot.reply_to(message, f"📸 @{username} kontrol ediliyor...")
            process_manual_ig(message, wait, username)
            return

    msg = bot.reply_to(
        message,
        "📸 Instagram kullanıcı adını gönder:\n"
        "Örnek: <code>bpthaber</code> veya <code>https://instagram.com/bpthaber</code>"
    )
    bot.register_next_step_handler(msg, handle_manual_ig)

def handle_manual_ig(message: types.Message):
    username = extract_ig_username(message.text or "")
    if not username:
        bot.reply_to(message, "❌ Geçersiz kullanıcı adı!")
        return

    wait = bot.reply_to(message, f"📸 @{username} kontrol ediliyor...")
    process_manual_ig(message, wait, username)

def process_manual_ig(message: types.Message, wait, username: str):
    try:
        ig_login()
    except Exception as e:
        bot.edit_message_text(
            f"❌ Instagram oturumu açılamadı: {e}",
            chat_id=wait.chat.id,
            message_id=wait.message_id
        )
        return

    try:
        medias = ig_get_recent_medias(username)
    except Exception as e:
        bot.edit_message_text(
            f"❌ {e}",
            chat_id=wait.chat.id,
            message_id=wait.message_id
        )
        return

    if not medias:
        bot.edit_message_text(
            f"📸 @{username} için son {FETCH_WINDOW_MIN} dakikada yeni gönderi yok.",
            chat_id=wait.chat.id,
            message_id=wait.message_id
        )
        return

    bot.edit_message_text(
        f"📸 {len(medias)} gönderi bulundu, gönderiliyor...",
        chat_id=wait.chat.id,
        message_id=wait.message_id
    )

    # Manuel komutta label = @username (istersen burayı da kanal adı yapabiliriz)
    for m in medias:
        cap = format_ig_caption(m, f"@{username}")
        items = media_to_items(m)
        send_media_to_channel(message.chat.id, items, cap)

# ========= OTOMATİK İÇERİK ÇEKME =========
def auto_fetch_channels():
    """Tüm kanalları otomatik kontrol et"""
    while True:
        try:
            channels = load_channels()

            if not channels:
                time.sleep(CHECK_INTERVAL_MIN * 60)
                continue

            # Instagram'a giriş yap
            try:
                ig_login()
            except Exception as e:
                print(f"❌ Instagram login hatası: {e}")
                time.sleep(CHECK_INTERVAL_MIN * 60)
                continue

            # Her kanalı ve o kanala bağlı IG profillerini kontrol et
            for ch_id, data in channels.items():
                ig_accounts = data.get('ig_accounts') or {}
                if not ig_accounts:
                    continue

                for ig_user, info in ig_accounts.items():
                    try:
                        medias = ig_get_recent_medias(ig_user)
                        if not medias:
                            continue

                        last_post_id = (info or {}).get('last_post_id')
                        new_medias = []

                        for m in medias:
                            if last_post_id and str(m.pk) == str(last_post_id):
                                break
                            new_medias.append(m)

                        if new_medias:
                            new_medias.reverse()

                            for m in new_medias:
                                # OTOMATİKTE LABEL = KANAL ADI
                                cap = format_ig_caption(m, data['title'])
                                items = media_to_items(m)
                                send_media_to_channel(int(ch_id), items, cap)
                                time.sleep(2)  # Rate limit önleme

                            channels[ch_id]['ig_accounts'][ig_user]['last_post_id'] = str(medias[0].pk)
                            save_channels(channels)

                            print(f"✅ {data['title']} kanalına @{ig_user} için {len(new_medias)} gönderi gönderildi")

                    except Exception as e:
                        print(f"❌ {data['title']} / @{ig_user} kontrol hatası: {e}")

                    time.sleep(3)  # IG hesapları arası bekleme

        except Exception as e:
            print(f"❌ Otomatik kontrol hatası: {e}")

        time.sleep(CHECK_INTERVAL_MIN * 60)

# ========= ÇALIŞTIR =========
if __name__ == "__main__":
    print(f"{BOT_NAME} başladı!")
    print(f"📸 Instagram kontrol aralığı: {CHECK_INTERVAL_MIN} dakika")
    print(f"⏰ Gönderi zaman penceresi: {FETCH_WINDOW_MIN} dakika")
    print("Komutlar: /ekle, /liste, /sec, /sil, /ig, /getid")

    auto_thread = threading.Thread(target=auto_fetch_channels, daemon=True)
    auto_thread.start()

    try:
        bot.infinity_polling(skip_pending=True)
    except KeyboardInterrupt:
        print("\nBot durduruldu.")
        pass
