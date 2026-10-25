"""
MovieVC Bot - Telegram group video chat me movie play karne wala bot (4 ghante tak ki movie support)
Stack : Pyrogram fork (Bot API 9.4 colored buttons) + py-tgcalls 2.x + yt-dlp + ffmpeg
"""
import asyncio
import html
import logging
import os
import re
import shutil
import time
from typing import Dict, List, Optional

import yt_dlp
from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream, VideoQuality

try:
    from pytgcalls.types import VideoParameters
except Exception:  # pragma: no cover
    VideoParameters = None

try:  # colored buttons (Bot API 9.4) - sirf supported forks me hota hai
    from pyrogram.enums import ButtonStyle
except Exception:  # pragma: no cover
    ButtonStyle = None

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("MovieVC")

# ───────────────────────── CONFIG ─────────────────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]          # assistant account (user) ka pyrogram session
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SUPPORT_CHAT = os.getenv("SUPPORT_CHAT", "")            # e.g. https://t.me/yourchat (optional)
MAX_DURATION = int(os.getenv("MAX_DURATION", str(4 * 3600)))   # 4 hours


def _video_params():
    """VIDEO_RES=640x360 + VIDEO_FPS=24 de sakte ho; warna VIDEO_QUALITY (SD_480p/HD_720p/FHD_1080p)"""
    res = os.getenv("VIDEO_RES", "").lower().strip()
    if res and VideoParameters is not None:
        try:
            w, h = res.split("x")
            return VideoParameters(width=int(w), height=int(h), frame_rate=int(os.getenv("VIDEO_FPS", "24")))
        except Exception as e:
            log.warning("VIDEO_RES use nahi ho paya (%s), VIDEO_QUALITY use kar raha hu", e)
    return getattr(VideoQuality, os.getenv("VIDEO_QUALITY", "SD_480p"), VideoQuality.SD_480p)


VIDEO_QUALITY = _video_params()
COOKIES_FILE = os.getenv("COOKIES_FILE", "")            # optional yt-dlp cookies
MODE = os.getenv("MODE", "stream").lower()              # stream = disk me download NAHI (Heroku ke liye best) | download = pehle poori file download
STREAM_PORT = int(os.getenv("STREAM_PORT", "8765"))     # internal localhost port (bahar se access nahi hota)
KEEP_FILES = os.getenv("KEEP_FILES", "0") == "1"        # 1 = downloaded movie delete nahi hogi (dobara /play pe instant start)
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
SUDO = {OWNER_ID} if OWNER_ID else set()

# ─────────────── PREMIUM EMOJI (yahan apne IDs daalo) ───────────────
# format: key -> (custom_emoji_id, fallback_emoji)   fallback hamesha SINGLE emoji rakho
# ID nikalne ke liye bot me premium emoji bhejke reply me /emojiid do
_DEFAULT = "5368324170671202286"
E = {
    "movie":   (_DEFAULT, "🎬"),
    "play":    (_DEFAULT, "▶️"),
    "pause":   (_DEFAULT, "⏸"),
    "stop":    (_DEFAULT, "⏹"),
    "skip":    (_DEFAULT, "⏭"),
    "back":    (_DEFAULT, "⏪"),
    "fwd":     (_DEFAULT, "⏩"),
    "refresh": (_DEFAULT, "🔄"),
    "time":    (_DEFAULT, "⏱"),
    "user":    (_DEFAULT, "👤"),
    "ok":      (_DEFAULT, "✅"),
    "warn":    (_DEFAULT, "⚠️"),
    "popcorn": (_DEFAULT, "🍿"),
    "add":     (_DEFAULT, "➕"),
    "queue":   (_DEFAULT, "📜"),
    "load":    (_DEFAULT, "⏳"),
}


def pe(key: str) -> str:
    """premium emoji tag for message text"""
    eid, fb = E[key]
    return f'<emoji id="{eid}">{fb}</emoji>'


def strip_emoji(text: str) -> str:
    return re.sub(r'<emoji id="\d+">(.*?)</emoji>', r"\1", text)


def btn(text: str, data: str = None, url: str = None, style: str = None, emoji: str = None):
    """colored + premium-emoji inline button (unsupported fork par plain button ban jata hai)"""
    kw = {}
    if style and ButtonStyle is not None:
        kw["style"] = getattr(ButtonStyle, style.upper(), style)
    if emoji:
        kw["icon_custom_emoji_id"] = E[emoji][0]
    if data:
        kw["callback_data"] = data
    if url:
        kw["url"] = url
    try:
        return InlineKeyboardButton(text, **kw)
    except TypeError:
        kw.pop("style", None)
        kw.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(text, **kw)


# ───────────────────────── CLIENTS ─────────────────────────
bot = Client("MovieBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             parse_mode=ParseMode.HTML, in_memory=True)
assistant = Client("MovieAssistant", api_id=API_ID, api_hash=API_HASH,
                   session_string=SESSION_STRING, in_memory=True)
call = PyTgCalls(assistant)


# ───────────────────────── SAFE SEND HELPERS ─────────────────────────
async def reply(m: Message, text: str, markup=None):
    try:
        return await m.reply_text(text, reply_markup=markup)
    except Exception as e:
        if "ENTITY" in str(e).upper() or "EMOJI" in str(e).upper():
            return await m.reply_text(strip_emoji(text), reply_markup=markup)
        raise


async def send(chat_id: int, text: str, markup=None):
    try:
        return await bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        if "ENTITY" in str(e).upper() or "EMOJI" in str(e).upper():
            return await bot.send_message(chat_id, strip_emoji(text), reply_markup=markup)
        raise


async def edit(msg: Optional[Message], text: str, markup=None):
    if not msg:
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception as e:
        s = str(e).upper()
        if "MESSAGE_NOT_MODIFIED" in s:
            return
        if "ENTITY" in s or "EMOJI" in s:
            try:
                await msg.edit_text(strip_emoji(text), reply_markup=markup)
            except Exception:
                pass


# ───────────────────────── SESSION STATE ─────────────────────────
class Session:
    def __init__(self, chat_id, title, source, duration, local, requester_id, requester_name):
        self.chat_id = chat_id
        self.title = title
        self.source = source            # direct url ya local file path
        self.duration = duration        # seconds (0 = unknown)
        self.local = local              # True => downloaded file
        self.requester_id = requester_id
        self.requester_name = requester_name
        self.offset = 0                 # kis second se stream start hui
        self.started = 0.0
        self.paused = False
        self.paused_at = 0.0
        self.paused_total = 0.0
        self.retries = 0
        self.info = ""
        self.heavy = False
        self.busy = False
        self.stopped = False
        self.panel: Optional[Message] = None
        self.task: Optional[asyncio.Task] = None

    def position(self) -> float:
        if not self.started:
            return float(self.offset)
        ref = self.paused_at if self.paused else time.monotonic()
        pos = self.offset + (ref - self.started - self.paused_total)
        if self.duration:
            pos = min(pos, self.duration)
        return max(pos, 0.0)


sessions: Dict[int, Session] = {}
queues: Dict[int, List[Session]] = {}


def hms(s) -> str:
    s = int(max(s, 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def bar(pos, dur, n=18) -> str:
    if not dur:
        return "▱" * n
    f = int(n * min(pos / dur, 1))
    return "▰" * f + "▱" * (n - f)


def parse_time(t: str) -> Optional[int]:
    try:
        parts = [int(x) for x in t.split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def panel_text(s: Session) -> str:
    pos = s.position()
    state = "Paused" if s.paused else "Now Playing"
    icon = pe("pause") if s.paused else pe("movie")
    qn = len(queues.get(s.chat_id, []))
    extra = ""
    if s.info:
        extra += f"{pe('movie')} Source: <code>{html.escape(s.info)}</code>\n"
    if s.heavy:
        extra += f"{pe('warn')} Source heavy hai (HEVC/1080p+), server CPU kam pada to video atak sakti hai.\n"
    txt = (
        f"{icon} <b>{state}</b>\n\n"
        f"{pe('popcorn')} <b>{html.escape(s.title[:80])}</b>\n\n"
        f"{extra}\n"
        f"{pe('time')} <code>{hms(pos)}</code> / <code>{hms(s.duration) if s.duration else 'LIVE/?'}</code>\n"
        f"<code>{bar(pos, s.duration)}</code>\n\n"
        f"{pe('user')} Requested by: <a href=\"tg://user?id={s.requester_id}\">{html.escape(s.requester_name)}</a>"
    )
    if qn:
        txt += f"\n{pe('queue')} Queue me: <b>{qn}</b>"
    return txt


def panel_kb(s: Session) -> InlineKeyboardMarkup:
    c = s.chat_id
    mid = (btn("Resume", f"mv:resume:{c}", style="success", emoji="play") if s.paused
           else btn("Pause", f"mv:pause:{c}", style="primary", emoji="pause"))
    return InlineKeyboardMarkup([
        [btn("-10 min", f"mv:back:{c}", style="primary", emoji="back"),
         mid,
         btn("+10 min", f"mv:fwd:{c}", style="primary", emoji="fwd")],
        [btn("Skip", f"mv:skip:{c}", style="primary", emoji="skip"),
         btn("Refresh", f"mv:refresh:{c}", style="primary", emoji="refresh"),
         btn("Stop", f"mv:stop:{c}", style="danger", emoji="stop")],
    ])


# ───────────────────────── PYTGCALLS WRAPPERS ─────────────────────────
def _api(*names):
    for n in names:
        f = getattr(call, n, None)
        if f:
            return f
    raise RuntimeError(f"py-tgcalls me {names} method nahi mila - version check karo")


FF_NET = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def build_stream(s: Session, start: int = 0) -> MediaStream:
    params = []
    if not s.local:
        params.append(FF_NET)
    if start > 0:
        params.append(f"-ss {int(start)}")
    return MediaStream(
        s.source,
        audio_parameters=AudioQuality.HIGH,
        video_parameters=VIDEO_QUALITY,
        ffmpeg_parameters=" ".join(params) or None,
    )


async def start_stream(s: Session, start: int = 0):
    await _api("play", "join_group_call")(s.chat_id, build_stream(s, start))
    s.offset = int(start)
    s.started = time.monotonic()
    s.paused = False
    s.paused_total = 0.0


async def leave(chat_id: int):
    try:
        await _api("leave_call", "leave_group_call")(chat_id)
    except Exception:
        pass


# ───────────────────────── MEDIA HELPERS ─────────────────────────
DIRECT_RE = re.compile(r"\.(mp4|mkv|webm|avi|mov|m3u8|ts|flv)(\?|$)", re.I)


async def probe_duration(src: str) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", src,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), 40)
        return int(float(out.decode().strip()))
    except Exception:
        return 0


async def probe_video(src: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,pix_fmt,avg_frame_rate",
            "-of", "default=nw=1", src,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), 40)
        d = dict(l.split("=", 1) for l in out.decode().splitlines() if "=" in l)
        if not d:
            return "NO VIDEO STREAM"
        num, den = (d.get("avg_frame_rate", "0/1").split("/") + ["1"])[:2]
        fps = round(int(num) / max(int(den), 1))
        return f"{d.get('codec_name', '?').upper()} {d.get('width', '?')}x{d.get('height', '?')} {fps}fps {d.get('pix_fmt', '')}".strip()
    except Exception:
        return ""


async def fill_info(s: "Session"):
    s.info = await probe_video(s.source)
    if not s.info:
        return
    log.info("Source video: %s | output: %s", s.info, os.getenv("VIDEO_RES") or os.getenv("VIDEO_QUALITY", "SD_480p"))
    try:
        h = int(s.info.split()[1].split("x")[1])
    except Exception:
        h = 0
    s.heavy = ("HEVC" in s.info) or ("AV1" in s.info) or h >= 1080 or "10LE" in s.info.upper()
    await refresh_panel(s)


async def resolve_link(url: str):
    """return (stream_url, title, duration)"""
    if DIRECT_RE.search(url):
        title = os.path.basename(url.split("?")[0]) or "Movie"
        return url, title, await probe_duration(url)

    def _extract():
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "format": "best[height<=720]/best", "skip_download": True}
        if COOKIES_FILE:
            opts["cookiefile"] = COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as y:
            return y.extract_info(url, download=False)

    info = await asyncio.to_thread(_extract)
    link = info.get("url") or (info.get("requested_formats") or [{}])[0].get("url")
    if not link:
        raise RuntimeError("Direct stream link nahi mila")
    dur = int(info.get("duration") or 0) or await probe_duration(link)
    return link, info.get("title") or "Movie", dur


def drop_file(s: Session):
    if KEEP_FILES:
        return
    if s.local and os.path.exists(s.source):
        try:
            os.remove(s.source)
        except Exception:
            pass


async def ensure_assistant(chat) -> bool:
    try:
        await assistant.get_chat_member(chat.id, "me")
        return True
    except Exception:
        pass
    try:
        target = chat.username or await bot.export_chat_invite_link(chat.id)
        await assistant.join_chat(target)
        return True
    except Exception as e:
        if "USER_ALREADY_PARTICIPANT" in str(e).upper():
            return True
        log.warning("assistant join failed: %s", e)
        return False


async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in SUDO:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


# ───────────────────────── TELEGRAM FILE -> LOCAL HTTP STREAM ─────────────────────────
# Disk me download kiye bina: assistant Telegram se chunks laata hai, ffmpeg localhost se padhta hai.
CHUNK = 1024 * 1024
_msg_cache: Dict[tuple, tuple] = {}


async def _get_msg(chat_id: int, msg_id: int):
    key = (chat_id, msg_id)
    hit = _msg_cache.get(key)
    if hit and time.time() - hit[0] < 1800:       # file_reference expire na ho isliye 30 min cache
        return hit[1]
    msg = await assistant.get_messages(chat_id, msg_id)
    _msg_cache[key] = (time.time(), msg)
    return msg


async def serve_media(request: web.Request):
    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
        msg = await _get_msg(chat_id, msg_id)
        media = msg.video or msg.document
        size = media.file_size
        mime = getattr(media, "mime_type", None) or "video/mp4"
    except Exception as e:
        log.warning("serve_media: %s", e)
        return web.Response(status=404)

    start, end = 0, size - 1
    rng = request.headers.get("Range")
    if rng:
        mm = re.match(r"bytes=(\d*)-(\d*)", rng)
        if mm:
            if mm.group(1):
                start = int(mm.group(1))
                if mm.group(2):
                    end = min(int(mm.group(2)), size - 1)
            elif mm.group(2):                      # suffix range: last N bytes
                start = max(size - int(mm.group(2)), 0)
    if start >= size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})

    headers = {"Content-Type": mime, "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp = web.StreamResponse(status=206 if rng else 200, headers=headers)
    await resp.prepare(request)

    skip = start % CHUNK
    remaining = end - start + 1
    try:
        async for chunk in assistant.stream_media(msg, offset=start // CHUNK):
            if skip:
                chunk, skip = chunk[skip:], 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            await resp.write(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except (ConnectionResetError, ConnectionError):
        pass                                       # ffmpeg ne connection band kiya (seek) - normal
    except Exception as e:
        log.warning("serve_media stream error: %s", e)
    return resp


async def start_media_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/media/{chat_id}/{msg_id}", serve_media)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", STREAM_PORT).start()
    log.info("Media stream server on 127.0.0.1:%s (MODE=%s)", STREAM_PORT, MODE)
    return runner


# ───────────────────────── PLAYBACK CORE ─────────────────────────
async def begin(s: Session) -> bool:
    cid = s.chat_id
    sessions[cid] = s
    try:
        await start_stream(s, 0)
    except Exception as e:
        sessions.pop(cid, None)
        drop_file(s)
        if type(e).__name__ == "NoActiveGroupCall":
            txt = f"{pe('warn')} Pehle group me <b>Video Chat start</b> karo, phir /play do."
        else:
            txt = f"{pe('warn')} Stream start nahi hui:\n<code>{html.escape(str(e)[:300])}</code>"
        await send(cid, txt)
        return False
    s.panel = await send(cid, panel_text(s), panel_kb(s))
    s.task = asyncio.create_task(panel_loop(s))
    asyncio.create_task(fill_info(s))
    return True


async def finish(s: Session, note: str):
    s.stopped = True
    if s.task and s.task is not asyncio.current_task():
        s.task.cancel()
    drop_file(s)
    await edit(s.panel, f"{pe('ok')} <b>{note}</b>\n\n{pe('popcorn')} {html.escape(s.title[:80])}", None)


async def play_next(chat_id: int):
    old = sessions.pop(chat_id, None)
    if old:
        await finish(old, "Movie khatam")
    q = queues.get(chat_id) or []
    while q:
        nxt = q.pop(0)
        if await begin(nxt):
            return
    queues.pop(chat_id, None)
    await leave(chat_id)


async def stop_session(chat_id: int, note: str = "Stopped"):
    for qs in queues.pop(chat_id, []):
        drop_file(qs)
    s = sessions.pop(chat_id, None)
    if s:
        await finish(s, note)
    await leave(chat_id)


async def refresh_panel(s: Session):
    await edit(s.panel, panel_text(s), panel_kb(s))


async def panel_loop(s: Session):
    """har 45s panel update + 4 ghante ka hard limit watchdog"""
    try:
        while not s.stopped:
            await asyncio.sleep(45)
            if s.stopped or sessions.get(s.chat_id) is not s:
                break
            if s.position() > MAX_DURATION + 600:
                await send(s.chat_id, f"{pe('warn')} Max limit ({hms(MAX_DURATION)}) poori ho gayi, stream band kar raha hu.")
                await stop_session(s.chat_id, "Time limit reached")
                break
            if not s.paused:
                await refresh_panel(s)
    except asyncio.CancelledError:
        pass


async def handle_end(chat_id: int):
    s = sessions.get(chat_id)
    if not s or s.stopped or s.busy:
        return
    if time.monotonic() - s.started < 8:      # seek/restart ke fake end events ignore
        return
    s.busy = True
    try:
        pos = s.position()
        # movie beech me ruk gayi (network drop etc.) => wahin se resume (max 3 baar)
        if s.duration and pos < s.duration - 90 and s.retries < 3:
            s.retries += 1
            await start_stream(s, max(int(pos) - 3, 0))
            await send(chat_id, f"{pe('warn')} Stream beech me ruki thi, <code>{hms(pos)}</code> se dobara start ki ({s.retries}/3).")
            return
        await play_next(chat_id)
    except Exception as e:
        log.exception("handle_end: %s", e)
    finally:
        s.busy = False


@call.on_update()
async def _on_update(_, update):
    if type(update).__name__ in ("StreamEnded", "StreamAudioEnded", "StreamVideoEnded"):
        await handle_end(update.chat_id)


async def enqueue(m: Message, source: str, title: str, duration: int, local: bool):
    cid = m.chat.id
    s = Session(cid, title, source, duration, local, m.from_user.id if m.from_user else 0,
                m.from_user.first_name if m.from_user else "User")
    if duration and duration > MAX_DURATION:
        drop_file(s)
        return await reply(m, f"{pe('warn')} Movie <b>{hms(duration)}</b> ki hai, max allowed <b>{hms(MAX_DURATION)}</b>.")
    if cid in sessions:
        queues.setdefault(cid, []).append(s)
        return await reply(m, f"{pe('queue')} Queue me add hui (#{len(queues[cid])}): <b>{html.escape(title[:70])}</b>")
    await begin(s)


# ───────────────────────── COMMANDS ─────────────────────────
@bot.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    me = await bot.get_me()
    rows = [[btn("Add me to your group",
                 url=f"https://t.me/{me.username}?startgroup=true&admin=manage_video_chats+invite_users+delete_messages",
                 style="success", emoji="add")]]
    if SUPPORT_CHAT:
        rows.append([btn("Support", url=SUPPORT_CHAT, style="primary", emoji="movie")])
    await reply(m,
        f"{pe('movie')} <b>Welcome {html.escape(m.from_user.first_name)}!</b>\n\n"
        f"{pe('popcorn')} Main group ke <b>Video Chat</b> me movie stream kar sakta hu — <b>{hms(MAX_DURATION)}</b> tak ki.\n\n"
        f"{pe('play')} <code>/play &lt;link&gt;</code> — direct link / YouTube / m3u8\n"
        f"{pe('play')} Video file pe reply karke <code>/play</code>\n"
        f"{pe('pause')} /pause /resume /skip /stop\n"
        f"{pe('fwd')} <code>/seek 1:20:30</code> — us time pe jao",
        InlineKeyboardMarkup(rows))


@bot.on_message(filters.command(["play", "movie"]) & filters.group)
async def play_cmd(_, m: Message):
    cid = m.chat.id
    r = m.reply_to_message
    media = None
    if r:
        media = r.video or (r.document if r.document and (r.document.mime_type or "").startswith("video") else None)

    url = None
    if len(m.command) > 1:
        url = m.command[1]
    elif r and r.text and re.match(r"https?://", r.text.strip()):
        url = r.text.strip().split()[0]

    if not media and not url:
        return await reply(m, f"{pe('warn')} Usage: <code>/play &lt;link&gt;</code> ya kisi video file pe reply karke /play")

    status = await reply(m, f"{pe('load')} Assistant check ho raha hai...")
    if not await ensure_assistant(m.chat):
        return await edit(status, f"{pe('warn')} Assistant group me join nahi ho paya. Bot ko <b>Invite Users</b> admin right do ya assistant ko manually add karo.")

    try:
        if media and MODE == "stream":
            title = getattr(media, "file_name", None) or "Telegram Movie"
            src = f"http://127.0.0.1:{STREAM_PORT}/media/{cid}/{r.id}"
            duration = int(getattr(media, "duration", 0) or 0) or await probe_duration(src)
            await status.delete()
            await enqueue(m, src, title, duration, False)
        elif media:
            size = getattr(media, "file_size", 0) or 0
            ext = os.path.splitext(getattr(media, "file_name", "") or "")[1] or ".mp4"
            path = os.path.join(DOWNLOAD_DIR, f"{cid}_{media.file_unique_id}{ext}")
            cached = os.path.exists(path) and size and os.path.getsize(path) >= size

            if not cached:
                if size > shutil.disk_usage(DOWNLOAD_DIR).free - 300 * 1024 * 1024:
                    return await edit(status, f"{pe('warn')} Server pe itni disk space nahi hai is file ke liye.")
                last = [0.0]

                async def prog(cur, total):
                    if time.time() - last[0] < 8:
                        return
                    last[0] = time.time()
                    await edit(status,
                               f"{pe('load')} Movie download ho rahi hai...\n"
                               f"<b>{cur * 100 // max(total, 1)}%</b> — "
                               f"<code>{cur // 1048576} / {total // 1048576} MB</code>")

                # bade (2GB+) file ke liye assistant (user account) se download - bot se zyada reliable
                try:
                    am = await assistant.get_messages(cid, r.id)
                    path = await assistant.download_media(am, file_name=path, progress=prog)
                except Exception as e:
                    log.warning("assistant download fail (%s), bot se try kar raha hu", e)
                    path = await r.download(file_name=path, progress=prog)
            title = getattr(media, "file_name", None) or "Telegram Movie"
            duration = int(getattr(media, "duration", 0) or 0) or await probe_duration(path)
            await status.delete()
            await enqueue(m, path, title, duration, True)
        else:
            await edit(status, f"{pe('load')} Link process ho raha hai...")
            src, title, duration = await resolve_link(url)
            await status.delete()
            await enqueue(m, src, title, duration, False)
    except Exception as e:
        log.exception("play_cmd")
        await edit(status, f"{pe('warn')} Error:\n<code>{html.escape(str(e)[:300])}</code>")


async def _admin_guard(m: Message) -> Optional[Session]:
    s = sessions.get(m.chat.id)
    if not s:
        await reply(m, f"{pe('warn')} Abhi koi movie play nahi ho rahi.")
        return None
    uid = m.from_user.id if m.from_user else 0
    if uid != s.requester_id and not await is_admin(m.chat.id, uid):
        await reply(m, f"{pe('warn')} Sirf admins ya requester ye kar sakte hain.")
        return None
    return s


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, m: Message):
    s = await _admin_guard(m)
    if s and not s.paused:
        await _api("pause", "pause_stream")(s.chat_id)
        s.paused, s.paused_at = True, time.monotonic()
        await refresh_panel(s)


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, m: Message):
    s = await _admin_guard(m)
    if s and s.paused:
        await _api("resume", "resume_stream")(s.chat_id)
        s.paused_total += time.monotonic() - s.paused_at
        s.paused = False
        await refresh_panel(s)


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
async def stop_cmd(_, m: Message):
    if await _admin_guard(m):
        await stop_session(m.chat.id)


@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, m: Message):
    if await _admin_guard(m):
        await play_next(m.chat.id)


@bot.on_message(filters.command("seek") & filters.group)
async def seek_cmd(_, m: Message):
    s = await _admin_guard(m)
    if not s:
        return
    t = parse_time(m.command[1]) if len(m.command) > 1 else None
    if t is None:
        return await reply(m, f"{pe('warn')} Usage: <code>/seek 1:20:30</code> (hh:mm:ss ya mm:ss)")
    await do_seek(s, t)
    await reply(m, f"{pe('fwd')} <code>{hms(s.position())}</code> pe jump kar diya.")


async def do_seek(s: Session, target: int):
    target = max(0, int(target))
    if s.duration:
        target = min(target, s.duration - 5)
    s.retries = 0
    await start_stream(s, target)
    await refresh_panel(s)


@bot.on_message(filters.command("emojiid"))
async def emojiid_cmd(_, m: Message):
    src = m.reply_to_message or m
    ents = (src.entities or []) + (src.caption_entities or [])
    ids = [e.custom_emoji_id for e in ents if str(e.type).upper().endswith("CUSTOM_EMOJI")]
    if not ids:
        return await reply(m, f"{pe('warn')} Premium emoji wale message pe reply karke /emojiid do.")
    await reply(m, "\n".join(f"<code>{i}</code>" for i in ids))


@bot.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    t = time.time()
    x = await reply(m, f"{pe('ok')} Pong!")
    await edit(x, f"{pe('ok')} Pong! <code>{int((time.time() - t) * 1000)} ms</code>")


# ───────────────────────── BUTTON CONTROLS ─────────────────────────
@bot.on_callback_query(filters.regex(r"^mv:"))
async def controls(_, q: CallbackQuery):
    _, act, cid = q.data.split(":")
    cid = int(cid)
    s = sessions.get(cid)
    if not s:
        return await q.answer("Koi movie play nahi ho rahi.", show_alert=True)
    if q.from_user.id != s.requester_id and not await is_admin(cid, q.from_user.id):
        return await q.answer("Sirf admins ya requester control kar sakte hain.", show_alert=True)
    try:
        if act == "pause" and not s.paused:
            await _api("pause", "pause_stream")(cid)
            s.paused, s.paused_at = True, time.monotonic()
        elif act == "resume" and s.paused:
            await _api("resume", "resume_stream")(cid)
            s.paused_total += time.monotonic() - s.paused_at
            s.paused = False
        elif act == "back":
            await do_seek(s, s.position() - 600)
        elif act == "fwd":
            await do_seek(s, s.position() + 600)
        elif act == "skip":
            await q.answer("Skipped")
            return await play_next(cid)
        elif act == "stop":
            await q.answer("Stopped")
            return await stop_session(cid)
        await q.answer("Updated")
        await refresh_panel(s)
    except Exception as e:
        log.exception("controls")
        await q.answer(f"Error: {str(e)[:150]}", show_alert=True)


# ───────────────────────── MAIN ─────────────────────────
async def main():
    await bot.start()
    await assistant.start()
    await call.start()
    runner = await start_media_server()
    me = await bot.get_me()
    log.info("MovieVC started as @%s", me.username)
    await idle()
    await runner.cleanup()
    await bot.stop()
    await assistant.stop()


if __name__ == "__main__":
    # Pyrogram Client import time par jo loop mila usi se bind ho jata hai,
    # isliye asyncio.run() (naya loop) use nahi karna - wahi loop chalana hai.
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
