import os
import re
import io
import uuid
import base64
import requests
from flask import Flask, request, jsonify

app = Flask(__name__, static_folder='../static', static_url_path='/')

# ============================================================
# KONFIGURASI — SEMUA KEY DIAMBIL DARI ENVIRONMENT VARIABLES.
# Jangan pernah hardcode key asli di file ini. Set lewat:
# Vercel Dashboard > Project > Settings > Environment Variables
#   MISTRAL_API_KEY
#   SUPABASE_URL
#   SUPABASE_SERVICE_KEY   (secret key, BUKAN publishable key)
# ============================================================
API_KEY = os.environ.get("MISTRAL_API_KEY")
API_URL = "https://api.mistral.ai/v1/chat/completions"
TEXT_MODEL = "mistral-large-latest"
VISION_MODEL = "pixtral-large-latest"

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
IMAGE_BUCKET = "user-images"
MAX_IMAGES_PER_USER = 10

FILE_BUCKET = "user-files"
FILE_QUOTA_DEFAULT = 500 * 1024 * 1024   # 500MB per akun biasa
FILE_QUOTA_ADMIN = 2 * 1024 * 1024 * 1024  # 2GB khusus admin
ADMIN_EMAIL = "gacoruncek73@gmail.com"

# Mode "RHF" — persona yang lebih santai, blak-blakan, gaya bebas.
# (Bukan mode buat menghapus batasan keamanan model — cuma beda gaya bicara.)
# SENGAJA DIBIARKAN KOSONG sesuai permintaan — jangan diisi di sini.
RHF_MODE = ""

NORMAL = ""


# ============================================================
# FIREBASE REALTIME DATABASE — dipakai khusus buat chat history
# ("ingatan" AI). Gambar & file tetap di Supabase Storage (di
# bawah), karena Firebase RTDB gratis untuk data kecil/realtime
# tapi mahal untuk storage file besar.
# Set FIREBASE_URL lewat environment variable juga kalau project
# id-nya beda; kalau kosong, fallback ke default project lama.
# ============================================================
FIREBASE_URL = (os.environ.get("FIREBASE_URL") or
                "https://rhf-zero-ai-default-rtdb.asia-southeast1.firebasedatabase.app").rstrip("/")


def firebase_get(path):
    try:
        r = requests.get(f"{FIREBASE_URL}/{path}.json", timeout=10)
        return r.json() if r.status_code == 200 else {}
    except requests.RequestException:
        return {}


def firebase_put(path, data):
    try:
        requests.put(f"{FIREBASE_URL}/{path}.json", json=data, timeout=10)
    except requests.RequestException:
        pass


def fb_get_messages(user_id, mode, limit=10):
    """Ambil history chat dari Firebase RTDB: sessions/{uid}/{mode} = [{role,content}, ...]"""
    history = firebase_get(f"sessions/{user_id}/{mode}")
    if not history or not isinstance(history, list):
        return []
    return history[-(limit * 2):]


def fb_append_messages(user_id, mode, user_content, assistant_content):
    """Tambah pasangan pesan user+assistant ke history, simpan max 20 terakhir."""
    history = firebase_get(f"sessions/{user_id}/{mode}")
    if not history or not isinstance(history, list):
        history = []
    if user_content:
        history.append({"role": "user", "content": user_content})
    history.append({"role": "assistant", "content": assistant_content})
    if len(history) > 20:
        history = history[-20:]
    firebase_put(f"sessions/{user_id}/{mode}", history)


# ============================================================
# SUPABASE HELPERS (lewat REST API langsung, tanpa SDK, pakai
# service_role key sehingga bypass Row Level Security).
# Dipakai khusus untuk penyimpanan gambar & file.
# ============================================================
def _sb_headers(extra=None):
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def sb_configured():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def sb_get_user_images(user_id, limit=MAX_IMAGES_PER_USER):
    """Metadata gambar milik user, terbaru dulu."""
    if not sb_configured():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/images",
            headers=_sb_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": str(limit),
                "select": "id,storage_path,mime_type,created_at",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        return []
    except requests.RequestException:
        return []


def sb_upload_image(user_id, raw_bytes, mime_type="image/jpeg"):
    """Upload gambar ke Storage bucket privat, simpan metadata, jaga max 10/akun."""
    if not sb_configured():
        return None
    ext = mime_type.split("/")[-1].replace("jpeg", "jpg")
    path = f"{user_id}/{uuid.uuid4().hex}.{ext}"
    try:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{IMAGE_BUCKET}/{path}",
            headers=_sb_headers({"Content-Type": mime_type}),
            data=raw_bytes,
            timeout=30,
        )
        if r.status_code not in (200, 201):
            return None
    except requests.RequestException:
        return None

    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/images",
            headers=_sb_headers({"Prefer": "return=minimal"}),
            json={"user_id": user_id, "storage_path": path, "mime_type": mime_type},
            timeout=10,
        )
    except requests.RequestException:
        pass

    # Bersihkan gambar lama kalau sudah lebih dari MAX_IMAGES_PER_USER
    _sb_prune_old_images(user_id)
    return path


def _sb_prune_old_images(user_id):
    imgs = sb_get_user_images(user_id, limit=1000)
    if len(imgs) <= MAX_IMAGES_PER_USER:
        return
    old = imgs[MAX_IMAGES_PER_USER:]
    for im in old:
        try:
            requests.delete(
                f"{SUPABASE_URL}/storage/v1/object/{IMAGE_BUCKET}/{im['storage_path']}",
                headers=_sb_headers(),
                timeout=10,
            )
            requests.delete(
                f"{SUPABASE_URL}/rest/v1/images",
                headers=_sb_headers(),
                params={"id": f"eq.{im['id']}"},
                timeout=10,
            )
        except requests.RequestException:
            pass


def sb_signed_url(storage_path, expires_in=3600):
    """Signed URL sementara buat kasih Pixtral akses baca gambar privat."""
    if not sb_configured():
        return None
    try:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/{IMAGE_BUCKET}/{storage_path}",
            headers=_sb_headers(),
            json={"expiresIn": expires_in},
            timeout=10,
        )
        if r.status_code == 200:
            signed_path = r.json().get("signedURL", "")
            return f"{SUPABASE_URL}/storage/v1{signed_path}" if signed_path else None
        return None
    except requests.RequestException:
        return None


# ============================================================
# FILE STORAGE (semua jenis file, bukan cuma gambar) — bucket
# terpisah `user-files`, tabel `files` di Supabase.
# ============================================================
def get_file_quota(user_email):
    return FILE_QUOTA_ADMIN if (user_email or "").lower() == ADMIN_EMAIL else FILE_QUOTA_DEFAULT


def sb_get_user_files(user_id, limit=1000):
    if not sb_configured():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/files",
            headers=_sb_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.asc",  # terlama duluan, buat prune
                "limit": str(limit),
                "select": "id,storage_path,file_name,mime_type,size_bytes,created_at",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        return []
    except requests.RequestException:
        return []


def sb_delete_file_row(file_id, storage_path):
    try:
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{FILE_BUCKET}/{storage_path}",
            headers=_sb_headers(),
            timeout=10,
        )
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/files",
            headers=_sb_headers(),
            params={"id": f"eq.{file_id}"},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _sb_enforce_file_quota(user_id, quota_bytes, incoming_size):
    """Hapus file terlama sampai ada cukup ruang buat file baru."""
    files = sb_get_user_files(user_id)
    total = sum(f.get("size_bytes", 0) for f in files)
    i = 0
    while total + incoming_size > quota_bytes and i < len(files):
        f = files[i]
        sb_delete_file_row(f["id"], f["storage_path"])
        total -= f.get("size_bytes", 0)
        i += 1
    return total + incoming_size <= quota_bytes


def sb_upload_file(user_id, user_email, raw_bytes, file_name, mime_type):
    if not sb_configured():
        return None, "Supabase belum dikonfigurasi."

    size = len(raw_bytes)
    quota = get_file_quota(user_email)
    if size > quota:
        return None, f"File terlalu besar untuk kuota akun ({quota // (1024*1024)}MB)."

    if not _sb_enforce_file_quota(user_id, quota, size):
        return None, "Kuota penyimpanan penuh, gagal membuat ruang meski file lama sudah dihapus."

    safe_name = re.sub(r"[^\w.\-]", "_", file_name or "file")
    path = f"{user_id}/{uuid.uuid4().hex}_{safe_name}"
    try:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{FILE_BUCKET}/{path}",
            headers=_sb_headers({"Content-Type": mime_type or "application/octet-stream"}),
            data=raw_bytes,
            timeout=60,
        )
        if r.status_code not in (200, 201):
            return None, f"Gagal upload ke storage: {r.status_code}"
    except requests.RequestException as e:
        return None, f"Gagal upload ke storage: {e}"

    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/files",
            headers=_sb_headers({"Prefer": "return=minimal"}),
            json={
                "user_id": user_id,
                "storage_path": path,
                "file_name": file_name,
                "mime_type": mime_type,
                "size_bytes": size,
            },
            timeout=10,
        )
    except requests.RequestException:
        pass

    return path, None


# ---- Deteksi format asli file lewat magic bytes (bukan cuma nama/ekstensi) ----
MAGIC_SIGNATURES = [
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # docx/xlsx/zip semua berbasis zip
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "audio/wav_or_video/avi"),
    (b"\x1f\x8b", "application/gzip"),
]


def detect_real_format(raw_bytes):
    """Cek magic bytes di awal file buat pastiin isi file memang sesuai
    klaimnya (bukan sekadar percaya ekstensi nama file dari user)."""
    head = raw_bytes[:16]
    for sig, kind in MAGIC_SIGNATURES:
        if head.startswith(sig):
            return kind
    # Coba deteksi teks polos (utf-8 decodable) buat .txt/.md/.csv/kode
    try:
        raw_bytes[:2048].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def extract_text_from_file(raw_bytes, file_name, mime_type):
    """Ekstrak isi jadi teks kalau tipenya bisa diparsing. Kalau tidak,
    kembalikan None (artinya AI cuma tahu file itu ada, bukan isinya)."""
    ext = (file_name or "").lower().rsplit(".", 1)[-1] if "." in (file_name or "") else ""
    real_kind = detect_real_format(raw_bytes)

    try:
        if ext == "pdf" or real_kind == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            return text.strip()[:20000] or None

        if ext == "docx":
            from docx import Document
            doc = Document(io.BytesIO(raw_bytes))
            text = "\n".join(p.text for p in doc.paragraphs)
            return text.strip()[:20000] or None

        if ext in ("xlsx", "xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
            lines = []
            for ws in wb.worksheets[:5]:
                lines.append(f"--- Sheet: {ws.title} ---")
                for row in ws.iter_rows(max_row=200, values_only=True):
                    lines.append(",".join("" if v is None else str(v) for v in row))
            return "\n".join(lines).strip()[:20000] or None

        if ext in ("txt", "md", "csv", "json", "py", "js", "html", "css", "yml", "yaml", "xml", "log") \
                or real_kind == "text/plain":
            return raw_bytes[:20000].decode("utf-8", errors="ignore").strip() or None

    except Exception as e:
        return f"(gagal membaca isi file: {e})"

    return None  # tipe biner yang nggak diparsing (video, audio, zip, dll)


# ============================================================
# TOOL: fetch_url — AI bisa minta baca isi sebuah URL yang
# dikirim/disebut user. Bukan search engine, cuma fetch langsung.
# ============================================================
FETCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Ambil dan baca isi teks dari sebuah URL di internet. Gunakan kalau user memberi link atau minta membaca sebuah halaman web.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL lengkap yang mau dibaca, contoh: https://example.com/artikel"}
            },
            "required": ["url"],
        },
    },
}


def do_fetch_url(url):
    if not re.match(r"^https?://", url or "", re.I):
        return "URL tidak valid, harus diawali http:// atau https://"
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RHF-Assistant/1.0)"},
        )
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:6000] if text else "(halaman kosong atau tidak bisa dibaca)"
    except requests.RequestException as e:
        return f"Gagal mengambil URL: {e}"


# ============================================================
# ROUTES
# ============================================================
@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/api/upload-file', methods=['POST'])
def upload_file():
    """Upload satu file (bukan gambar) ke Supabase Storage, validasi format
    lewat magic bytes, ekstrak isi teksnya kalau tipenya bisa diparsing."""
    body = request.get_json(silent=True) or {}
    session_id = body.get('session', '')
    user_email = body.get('email', '')
    file_name = body.get('file_name', 'file')
    mime_claimed = body.get('mime', 'application/octet-stream')
    b64 = re.sub(r'^data:[^;]+;base64,', '', body.get('data', '') or '')

    if not isinstance(session_id, str) or not session_id.strip() or '/' in session_id or '.' in session_id:
        return jsonify({'error': 'Session tidak valid.'}), 400
    if not b64:
        return jsonify({'error': 'File kosong.'}), 400

    try:
        raw = base64.b64decode(b64)
    except Exception:
        return jsonify({'error': 'Data file tidak valid (bukan base64 yang benar).'}), 400

    # --- Validasi format dulu: cek isi file sungguhan lewat magic bytes,
    #     bukan cuma percaya nama/ekstensi yang dikirim klien. ---
    real_kind = detect_real_format(raw)
    mime_to_store = mime_claimed if mime_claimed and mime_claimed != 'application/octet-stream' else real_kind

    path, err = sb_upload_file(session_id, user_email, raw, file_name, mime_to_store)
    if err:
        return jsonify({'error': err}), 400

    extracted = extract_text_from_file(raw, file_name, mime_to_store)

    return jsonify({
        'ok': True,
        'storage_path': path,
        'file_name': file_name,
        'size_bytes': len(raw),
        'detected_format': real_kind,
        'readable': extracted is not None,
        'preview': (extracted[:400] if extracted else None),
    })


@app.route('/api/ask', methods=['POST'])
def ask():
    if not API_KEY:
        return jsonify({'response': 'Server belum dikonfigurasi: MISTRAL_API_KEY belum diset di environment variables.'}), 500

    body = request.get_json(silent=True) or {}
    q = body.get('question', '') or body.get('message', '')
    mode = body.get('mode', 'normal')
    images_in = body.get('images', [])  # list of {data: base64, mime: 'image/png'} ATAU list of string base64
    use_memory_images = bool(body.get('use_memory_images', False))  # pakai 10 gambar lama user di sesi ini
    files_in = body.get('files', [])  # list of {data: base64, file_name, mime} — file non-gambar dikirim bareng pesan
    user_email = body.get('email', '')

    session_id = body.get('session', 'default')
    if not isinstance(session_id, str) or not session_id.strip() or '/' in session_id or '.' in session_id:
        session_id = 'default'

    if not q and not images_in and not files_in:
        return jsonify({'response': 'Pesan kosong.'}), 400

    # --- Simpan gambar baru yang diupload di request ini ke Supabase Storage ---
    uploaded_paths = []
    for img in images_in:
        if isinstance(img, dict):
            b64 = img.get('data', '')
            mime = img.get('mime', 'image/jpeg')
        else:
            b64 = img
            mime = 'image/jpeg'
        b64 = re.sub(r'^data:[^;]+;base64,', '', b64 or '')
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        path = sb_upload_image(session_id, raw, mime)
        if path:
            uploaded_paths.append(path)

    # --- Simpan & ekstrak file non-gambar yang dikirim bareng pesan ini ---
    file_context_blocks = []
    for f in files_in:
        if not isinstance(f, dict):
            continue
        b64 = re.sub(r'^data:[^;]+;base64,', '', f.get('data', '') or '')
        fname = f.get('file_name', 'file')
        mime_claimed = f.get('mime', 'application/octet-stream')
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        real_kind = detect_real_format(raw)
        mime_to_store = mime_claimed if mime_claimed and mime_claimed != 'application/octet-stream' else real_kind
        _path, _err = sb_upload_file(session_id, user_email, raw, fname, mime_to_store)
        extracted = extract_text_from_file(raw, fname, mime_to_store)
        if extracted:
            file_context_blocks.append(f"[Isi file '{fname}']\n{extracted}")
        else:
            file_context_blocks.append(f"[File '{fname}' diupload, tipe {real_kind}, isinya tidak bisa dibaca sebagai teks]")

    # --- Kumpulkan gambar yang perlu dikasih ke model vision:
    #     gambar baru di request ini, DAN (kalau diminta) gambar lama
    #     milik akun ini yang masih tersedia di sesi ini. ---
    image_urls_for_model = []
    for path in uploaded_paths:
        url = sb_signed_url(path)
        if url:
            image_urls_for_model.append(url)

    if use_memory_images and not uploaded_paths:
        for meta in sb_get_user_images(session_id, limit=MAX_IMAGES_PER_USER):
            url = sb_signed_url(meta['storage_path'])
            if url:
                image_urls_for_model.append(url)

    has_images = len(image_urls_for_model) > 0
    model = VISION_MODEL if has_images else TEXT_MODEL

    # --- Ambil history dari Supabase (gantiin Firebase RTDB) ---
    history = fb_get_messages(session_id, mode, limit=10)

    system = RHF_MODE if mode == 'rhf' else NORMAL

    messages = [{"role": "system", "content": system}]
    for h in history:
        if isinstance(h, dict) and 'role' in h and 'content' in h:
            messages.append({"role": h["role"], "content": h["content"]})

    # Gabungkan pertanyaan user dengan isi file yang berhasil diekstrak
    q_with_files = q
    if file_context_blocks:
        q_with_files = (q + "\n\n" if q else "") + "\n\n".join(file_context_blocks)

    # Pesan user: kalau ada gambar, format content jadi multi-part (teks + image_url)
    if has_images:
        content_parts = []
        if q_with_files:
            content_parts.append({"type": "text", "text": q_with_files})
        for url in image_urls_for_model:
            content_parts.append({"type": "image_url", "image_url": url})
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": q_with_files})

    tools = [FETCH_TOOL_SCHEMA] if not has_images else None  # vision model fokus baca gambar, skip tool call

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.9 if mode == 'rhf' else 0.7,
        "max_tokens": 2000,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        resp = _call_mistral(payload, messages)
    except requests.RequestException as e:
        return jsonify({'response': f'Gagal menghubungi Mistral API: {e}'}), 502

    # --- Simpan history teks ke Firebase RTDB ("ingatan" AI); gambar &
    #     file tetap disimpan terpisah di Supabase (tabel images/files) ---
    if q:
        user_log_text = q
    elif uploaded_paths and file_context_blocks:
        user_log_text = "[gambar + file]"
    elif uploaded_paths:
        user_log_text = "[gambar]"
    elif file_context_blocks:
        user_log_text = "[file]"
    else:
        user_log_text = ""
    fb_append_messages(session_id, mode, user_log_text, resp)

    return jsonify({'response': resp, 'images_saved': len(uploaded_paths), 'files_saved': len(file_context_blocks)})


def _call_mistral(payload, messages, depth=0):
    """Panggil Mistral, dan kalau model minta tool call (fetch_url),
    jalankan tool-nya lalu panggil lagi dengan hasilnya (max 3 putaran)."""
    r = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    if r.status_code != 200:
        return f"Error {r.status_code}: {r.text[:200]}"

    data = r.json()
    choice = data["choices"][0]["message"]

    tool_calls = choice.get("tool_calls")
    if tool_calls and depth < 3:
        messages.append(choice)
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name")
            args_raw = fn.get("arguments", "{}")
            try:
                import json as _json
                args = _json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except Exception:
                args = {}

            if name == "fetch_url":
                result = do_fetch_url(args.get("url", ""))
            else:
                result = f"Tool tidak dikenal: {name}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "name": name,
                "content": result,
            })

        payload["messages"] = messages
        return _call_mistral(payload, messages, depth=depth + 1)

    return choice.get("content", "") or "(tidak ada respons)"


# Vercel butuh variabel bernama `app` yang expose WSGI app di atas — sudah ada.
