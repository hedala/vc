import os
import asyncio
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
import speech_recognition as sr
from pydub import AudioSegment
from motor.motor_asyncio import AsyncIOMotorClient
import emoji
import tempfile
import logging
from concurrent.futures import ThreadPoolExecutor
import aiohttp
import backoff
import json
import speedtest
import time
from datetime import datetime, timedelta

# Temel konfigürasyon
API_ID = 6
API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"
BOT_TOKEN = ""

MONGO_URI = ""
client = AsyncIOMotorClient(MONGO_URI)
db = client.ai_bot_db
user_stats = db.user_stats
group_stats = db.group_stats
voice_task_stats = db.voice_task_stats

# Sabit değerler
OWNER_ID = 5646751940
LOG_GROUP_ID = -1001724115229

# Logging konfigürasyonu
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Client("voice_to_text_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

recognizer = sr.Recognizer()

# Eşzamanlı işlem sayısını sınırla
MAX_CONCURRENT_PROCESSES = 1000
semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROCESSES)

# Thread havuzu oluştur
thread_pool = ThreadPoolExecutor(max_workers=100)

# AI görevi kuyruğu ve işleyici
ai_task_queue = asyncio.Queue()
AI_CONCURRENCY_LIMIT = 100
ai_semaphore = asyncio.Semaphore(AI_CONCURRENCY_LIMIT)

def emo(emoji_name):
    """Emoji adını Unicode karakterine dönüştürür."""
    return emoji.emojize(f":{emoji_name}:", language='alias')

async def update_user_stats(user_id: int, username: str = None, first_name: str = None):
    await user_stats.update_one(
        {"user_id": user_id},
        {
            "$inc": {"usage_count": 1},
            "$set": {
                "username": username,
                "first_name": first_name,
                "last_used": datetime.now()
            }
        },
        upsert=True
    )

async def update_group_stats(group_id: int, group_title: str = None, group_username: str = None):
    await group_stats.update_one(
        {"group_id": group_id},
        {
            "$inc": {"usage_count": 1},
            "$set": {
                "group_title": group_title,
                "group_username": group_username,
                "last_used": datetime.now()
            }
        },
        upsert=True
    )

async def update_voice_task_stats():
    await voice_task_stats.update_one(
        {"_id": "voice_tasks"},
        {
            "$inc": {"total_tasks": 1},
            "$set": {"last_used": datetime.now()}
        },
        upsert=True
    )

async def send_log_message(message):
    try:
        await app.send_message(LOG_GROUP_ID, message)
    except Exception as e:
        logger.error(f"Log mesajı gönderilirken hata oluştu: {str(e)}")

async def log_error(error_message, user=None, chat=None, voice_file=None):
    log_text = f"{emo('warning')} **HATA RAPORU**\n\n"
    log_text += f"**Hata:** {error_message}\n\n"
    
    if user:
        log_text += f"**Kullanıcı Bilgileri:**\n"
        log_text += f"ID: `{user.id}`\n"
        if user.username:
            log_text += f"Kullanıcı Adı: @{user.username}\n"
        log_text += f"Ad: {user.first_name}\n\n"
    
    if chat:
        log_text += f"**Sohbet Bilgileri:**\n"
        log_text += f"ID: `{chat.id}`\n"
        if chat.username:
            log_text += f"Grup Kullanıcı Adı: @{chat.username}\n"
        if chat.title:
            log_text += f"Grup Adı: {chat.title}\n"
        if hasattr(chat, 'invite_link') and chat.invite_link:
            log_text += f"Grup Bağlantısı: {chat.invite_link}\n\n"
    
    if voice_file:
        log_text += f"**Ses Dosyası Bilgileri:**\n"
        log_text += f"Dosya ID: {voice_file.file_id}\n"
        log_text += f"Dosya Boyutu: {voice_file.file_size} bayt\n\n"
    
    log_text += f"**Tarih:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    await send_log_message(log_text)

def process_audio_file(file_path):
    try:
        with sr.AudioFile(file_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="tr-TR")
        return text
    except sr.UnknownValueError:
        return None
    except sr.RequestError:
        logger.error("Google Speech Recognition servisi şu anda kullanılamıyor.")
        return None

async def process_audio(file_path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(thread_pool, process_audio_file, file_path)

@backoff.on_exception(backoff.expo, aiohttp.ClientError, max_tries=5)
async def ddg_invoke(prompt):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "Accept": "text/event-stream",
        "Accept-Language": "en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://duckduckgo.com/",
        "Content-Type": "application/json",
        "Origin": "https://duckduckgo.com",
        "Connection": "keep-alive",
        "Cookie": "dcm=1",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Pragma": "no-cache",
        "TE": "trailers",
        "x-vqd-accept": "1",
        "Cache-Control": "no-store",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://duckduckgo.com/duckchat/v1/status", headers=headers
        ) as response:
            token = response.headers["x-vqd-4"]
            headers["x-vqd-4"] = token

        url = "https://duckduckgo.com/duckchat/v1/chat"
        data = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
        }

        async with session.post(url, headers=headers, json=data) as response:
            text = await response.text()

    ret = ""
    for line in text.split("\n"):
        if len(line) > 0 and line[6] == "{":
            dat = json.loads(line[6:])
            if "message" in dat:
                ret += dat["message"].replace("\\n", "\n")
    
    return ret.strip()

async def correct_text_with_ai(text):
    prompt = f"""Lütfen aşağıdaki metni kontrol et ve sadece gerekli düzeltmeleri yap. Anlam bozulmadıkça ve ciddi hatalar olmadıkça metne müdahale etme. Yalnızca düzeltilmiş metni döndür, başka açıklama ekleme.
    Orijinal metin: "{text}"
    
    Düzeltilmiş metin:"""
    
    async with ai_semaphore:
        return await ddg_invoke(prompt)

async def ai_task_processor():
    while True:
        task = await ai_task_queue.get()
        try:
            result = await task['func'](*task['args'])
            task['future'].set_result(result)
        except Exception as e:
            task['future'].set_exception(e)
        finally:
            ai_task_queue.task_done()

async def schedule_ai_task(func, *args):
    future = asyncio.Future()
    await ai_task_queue.put({'func': func, 'args': args, 'future': future})
    return await future

@app.on_message(filters.command("start"))
async def start_command(client, message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ses-Metin Dönüşümü Hakkında", callback_data="voice_to_text_info")]
    ])
    await message.reply_text(
        f"Merhaba! Ben ses mesajlarını metne çeviren ve AI ile düzelten bir botum. {emo('memo')} "
        f"Hemen bir ses mesajı göndermeye başlayabilirsiniz!",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^voice_to_text_info$"))
async def voice_to_text_info_callback(client, callback_query):
    info_text = f"""
    {emo('microphone')} Ses Mesajından Metne Çevirme Özelliği

    Kullanım:
    1. Bana bir ses mesajı gönderin.
    2. Ben bu ses mesajını hızlıca metne çevireceğim.
    3. Sonra, AI yardımıyla metni düzelteceğim.
    4. Size düzgün ve anlaşılır bir metin olarak geri göndereceğim.

    İpuçları:
    - Net ve anlaşılır konuşun.
    - Arka plan gürültüsünü minimumda tutun.
    - Türkçe konuşmalarınız için optimize edilmiştim.

    Not: AI düzeltmesi sayesinde, metin daha doğru ve anlaşılır olacaktır.
    """
    await callback_query.answer()
    await callback_query.message.edit_text(info_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Anladım", callback_data="understood")]]))

@app.on_callback_query(filters.regex("^understood$"))
async def understood_callback(client, callback_query):
    await callback_query.answer()
    await callback_query.message.edit_text(f"Harika! Şimdi bir ses mesajı göndermeyi deneyin. {emo('microphone')}")

@app.on_message(filters.voice)
async def voice_to_text(client: Client, message: Message):
    async with semaphore:
        try:
            user = message.from_user
            chat = message.chat
            voice_file = message.voice
            
            progress_message = await message.reply_text(
                f"{emo('hourglass_flowing_sand')} Ses dosyanız işleniyor...",
                quote=True
            )
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
                await message.download(file_name=temp_wav.name)
                audio = AudioSegment.from_file(temp_wav.name)
                audio.export(temp_wav.name, format="wav")
                
                text = await process_audio(temp_wav.name)

            if text:
                await progress_message.edit_text(f"{emo('memo')} Metin AI ile düzeltiliyor...")
                corrected_text = await schedule_ai_task(correct_text_with_ai, text)
                
                await progress_message.delete()
                await message.reply_text(
                    f"{emo('memo')} Sesten Yazıya:\n\n{corrected_text}",
                    quote=True,
                    parse_mode=ParseMode.MARKDOWN
                )
                await update_user_stats(user.id, user.username, user.first_name)
                if chat.type != "private":
                    await update_group_stats(chat.id, chat.title, chat.username)
                await update_voice_task_stats()
            else:
                await progress_message.delete()
                await message.reply_text(
                    f"{emo('warning')} Ses mesajı anlaşılamadı. Lütfen daha net bir şekilde konuşun ve tekrar deneyin.",
                    quote=True
                )

        except Exception as e:
            await log_error(str(e), user, chat, voice_file)
            await message.reply_text(f"{emo('warning')} Üzgünüm, ses işlenirken bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

        finally:
            if 'temp_wav' in locals():
                os.unlink(temp_wav.name)

@app.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_command(client, message):
    try:
        total_users = await user_stats.count_documents({})
        total_groups = await group_stats.count_documents({})
        total_voice_tasks = await voice_task_stats.find_one({"_id": "voice_tasks"})
        total_voice_tasks = total_voice_tasks["total_tasks"] if total_voice_tasks else 0

        top_users = await user_stats.find().sort("usage_count", -1).limit(10).to_list(length=10)
        top_groups = await group_stats.find().sort("usage_count", -1).limit(10).to_list(length=10)

        stats_message = f"{emo('bar_chart')} **Detaylı Bot İstatistikleri**\n\n"
        stats_message += f"**Genel İstatistikler:**\n"
        stats_message += f"Toplam Kullanıcı: {total_users:,}\n"
        stats_message += f"Toplam Grup: {total_groups:,}\n"
        stats_message += f"Toplam Ses Görevi: {total_voice_tasks:,}\n\n"

        stats_message += f"{emo('trophy')} **En Aktif 10 Kullanıcı**\n"
        for i, user in enumerate(top_users, 1):
            user_info = f"{i}. "
            if user.get('first_name'):
                user_info += f"{user['first_name']} "
            if user.get('username'):
                user_info += f"(@{user['username']}) "
            user_info += f"- ID: `{user['user_id']}` - Kullanım: {user['usage_count']:,}\n"
            stats_message += user_info

        stats_message += f"\n{emo('loudspeaker')} **En Aktif 10 Grup**\n"
        for i, group in enumerate(top_groups, 1):
            group_info = f"{i}. "
            if group.get('group_title'):
                group_info += f"{group['group_title']} "
            if group.get('group_username'):
                group_info += f"(@{group['group_username']}) "
            group_info += f"- ID: `{group['group_id']}` - Kullanım: {group['usage_count']:,}\n"
            stats_message += group_info

        # Kullanım trendi
        now = datetime.now()
        last_24h = await voice_task_stats.count_documents({"last_used": {"$gte": now - timedelta(hours=24)}})
        last_7d = await voice_task_stats.count_documents({"last_used": {"$gte": now - timedelta(days=7)}})
        last_30d = await voice_task_stats.count_documents({"last_used": {"$gte": now - timedelta(days=30)}})

        stats_message += f"\n{emo('chart_increasing')} **Kullanım Trendi**\n"
        stats_message += f"Son 24 saat: {last_24h:,} görev\n"
        stats_message += f"Son 7 gün: {last_7d:,} görev\n"
        stats_message += f"Son 30 gün: {last_30d:,} görev\n"

        await message.reply_text(stats_message)
    except Exception as e:
        error_message = f"İstatistik komutu hatası: {str(e)}"
        await log_error(error_message, message.from_user, message.chat)
        await message.reply_text(f"{emo('warning')} İstatistikler alınırken bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    start_time = time.time()
    ping_message = await message.reply_text(f"{emo('ping_pong')} Ping ölçülüyor...")
    end_time = time.time()
    ping_time = round((end_time - start_time) * 1000, 2)
    await ping_message.edit_text(f"{emo('ping_pong')} Ping: {ping_time} ms")

@app.on_message(filters.command("speed") & filters.user(OWNER_ID))
async def speed_test_command(client, message):
    speed_message = await message.reply_text(f"{emo('rocket')} Hız testi başlatılıyor...")
    
    try:
        st = speedtest.Speedtest()
        await speed_message.edit_text(f"{emo('globe_with_meridians')} En iyi sunucu seçiliyor...")
        st.get_best_server()
        
        await speed_message.edit_text(f"{emo('arrow_down')} İndirme hızı ölçülüyor...")
        download_speed = st.download() / 1_000_000  # Mbps cinsinden
        
        await speed_message.edit_text(f"{emo('arrow_up')} Yükleme hızı ölçülüyor...")
        upload_speed = st.upload() / 1_000_000  # Mbps cinsinden
        
        ping = st.results.ping
        
        result_message = (
            f"{emo('rocket')} **Hız Testi Sonuçları**\n\n"
            f"{emo('arrow_down')} İndirme Hızı: {download_speed:.2f} Mbps\n"
            f"{emo('arrow_up')} Yükleme Hızı: {upload_speed:.2f} Mbps\n"
            f"{emo('ping_pong')} Ping: {ping:.2f} ms"
        )
        
        await speed_message.edit_text(result_message)
    except Exception as e:
        error_message = f"Hız testi hatası: {str(e)}"
        await log_error(error_message, message.from_user, message.chat)
        await speed_message.edit_text(f"{emo('warning')} Hız testi sırasında bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def handle_private_message(client: Client, message: Message):
    await message.reply_text(
        f"{emo('information')} Merhaba! Ben sadece ses mesajlarını metne çevirebilir ve düzeltebilirim. "
        f"Lütfen bir ses mesajı gönderin, size hemen metne çevirilmiş ve düzeltilmiş halini ileteyim."
    )

async def main():
    await app.start()
    await send_log_message("Bot başlatıldı.")
    logger.info("Bot başlatıldı.")
    
    ai_processor_task = asyncio.create_task(ai_task_processor())
    
    try:
        await idle()
    finally:
        await app.stop()
        ai_processor_task.cancel()
        await send_log_message("Bot kapatılıyor.")
        logger.info("Bot kapatılıyor.")

if __name__ == "__main__":
    app.run(main())
