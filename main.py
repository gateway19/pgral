import os
import re
import base64
import json
import asyncio
import shutil
import webbrowser as wb
import argparse
from pathlib import Path
from mimetypes import guess_type
from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from io import BytesIO
from PIL import Image
from collections import OrderedDict
import sys
from concurrent.futures import ThreadPoolExecutor

# --- Ensure SVG is recognized ---
import mimetypes
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('image/svg+xml', '.svgz')

# --- Настройки ---
MAX_CACHE_SIZE_MB = 1024
MAX_CACHE_SIZE_BYTES = MAX_CACHE_SIZE_MB * 1024 * 1024
BATCH_SIZE = 150

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).parent.resolve()

BASE_DIR = get_base_dir()
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

app = FastAPI()

def get_templates_dir():
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "templates")
    else:
        return "templates"

templates = Jinja2Templates(directory=get_templates_dir())

# RAM caches
image_content_cache = OrderedDict()
preview_cache = OrderedDict()

# Thread pool for blocking I/O
_executor = ThreadPoolExecutor(max_workers=2)

def get_local_ip_addresses() -> list[str]:
    import socket
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        for info in addr_info:
            ip = info[4][0]
            if ip != "127.0.0.1" and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)

def get_cache_size(cache):
    return sum(len(content) for _, (_, content, _) in cache.items())

def evict_old_items(cache, max_size):
    while cache and get_cache_size(cache) > max_size:
        cache.popitem(last=False)

def normalize_path(path: str) -> str:
    return str(Path(path).resolve())

def get_full_image(norm_path: str):
    if norm_path in image_content_cache:
        item = image_content_cache.pop(norm_path)
        image_content_cache[norm_path] = item
        print(f"[CACHE] Full image: {norm_path}")
        return item[0], item[1]

    print(f"[DISK] Full image: {norm_path}")
    if not os.path.isfile(norm_path):
        raise FileNotFoundError()

    media_type, _ = guess_type(norm_path)
    if not media_type or not media_type.startswith('image/'):
        media_type = 'application/octet-stream'

    with open(norm_path, 'rb') as f:
        content = f.read()

    image_content_cache[norm_path] = (media_type, content, asyncio.get_event_loop().time())
    evict_old_items(image_content_cache, MAX_CACHE_SIZE_BYTES // 2)
    return media_type, content

def get_preview_image(norm_path: str, max_size=(512, 512)):
    if norm_path in preview_cache:
        item = preview_cache.pop(norm_path)
        preview_cache[norm_path] = item
        print(f"[CACHE] Preview: {norm_path}")
        return item[0], item[1]

    print(f"[DISK] Preview: {norm_path}")
    if not os.path.isfile(norm_path):
        raise FileNotFoundError()

    media_type, _ = guess_type(norm_path)
    if not media_type:
        media_type = 'application/octet-stream'

    # Handle SVG and other non-raster images directly
    if media_type == 'image/svg+xml':
        with open(norm_path, 'rb') as f:
            content = f.read()
        preview_cache[norm_path] = (media_type, content, asyncio.get_event_loop().time())
        evict_old_items(preview_cache, MAX_CACHE_SIZE_BYTES // 2)
        return media_type, content

    # Handle raster images with PIL
    try:
        with Image.open(norm_path) as img:
            if img.mode in ("RGBA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            if img.width > max_size[0] or img.height > max_size[1]:
                img.thumbnail(max_size, Image.LANCZOS)

            buf = BytesIO()
            img.save(buf, format='JPEG', quality=85)
            content = buf.getvalue()
    except Exception:
        # Fallback: serve original file if it's an image
        if media_type.startswith('image/'):
            with open(norm_path, 'rb') as f:
                content = f.read()
            preview_cache[norm_path] = (media_type, content, asyncio.get_event_loop().time())
            evict_old_items(preview_cache, MAX_CACHE_SIZE_BYTES // 2)
            return media_type, content
        else:
            raise

    preview_cache[norm_path] = ('image/jpeg', content, asyncio.get_event_loop().time())
    evict_old_items(preview_cache, MAX_CACHE_SIZE_BYTES // 2)
    return 'image/jpeg', content

# --- NEW: blocking scan moved to sync helper ---
def _scan_files_sync(folder: str, regex: str):
    normalized_folder = normalize_path(folder)
    if not os.path.isdir(normalized_folder):
        raise FileNotFoundError("Directory not found")

    pattern = re.compile(regex, re.IGNORECASE) if regex else None

    files = []
    for root, _, filenames in os.walk(normalized_folder):
        for name in filenames:
            full_path = os.path.join(root, name)
            if pattern is None or pattern.search(full_path):
                files.append(full_path)

    files.sort()
    return files

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    default_folder = r"C:\Users\admin19\Downloads"
    default_regex = r".*\.(png|jpg|jpeg|svg|webp|bmp|gif)$"
    save_mode = request.query_params.get("save") == "true"
    return templates.TemplateResponse("index.html", {
        "request": request,
        "default_folder": default_folder,
        "default_regex": default_regex,
        "save_mode": save_mode
    })

@app.post("/api/filter-paged")
async def api_filter_paged(
    folder: str = Form(...),
    regex: str = Form(""),
    offset: int = Form(0),
    limit: int = Form(BATCH_SIZE)
):
    try:
        loop = asyncio.get_event_loop()
        all_files = await loop.run_in_executor(_executor, _scan_files_sync, folder, regex)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}
    except FileNotFoundError as e:
        return {"error": "Directory not found"}
    except Exception as e:
        return {"error": f"Scan failed: {e}"}

    total = len(all_files)
    batch = all_files[offset:offset + limit]
    has_more = (offset + limit) < total

    return {
        "files": batch,
        "folder": folder,
        "regex": regex,
        "offset": offset + limit,
        "has_more": has_more,
        "total": total
    }

@app.get("/image/{b64_path}")
async def serve_full_image(b64_path: str):
    try:
        # Fix: add padding before decoding
        padded = b64_path + '=' * (-len(b64_path) % 4)
        path = base64.urlsafe_b64decode(padded).decode('utf-8')
        norm_path = normalize_path(path)
        media_type, content = get_full_image(norm_path)
        return Response(content=content, media_type=media_type)
    except Exception:
        return HTMLResponse("Image not available", status_code=404)

@app.get("/preview/{b64_path}")
async def serve_preview(b64_path: str):
    try:
        # Fix: add padding before decoding
        padded = b64_path + '=' * (-len(b64_path) % 4)
        path = base64.urlsafe_b64decode(padded).decode('utf-8')
        norm_path = normalize_path(path)
        media_type, content = get_preview_image(norm_path)
        return Response(content=content, media_type=media_type)
    except Exception:
        return HTMLResponse("Preview not available", status_code=404)

@app.get("/api/view")
async def api_view(data: str = Query(...)):
    try:
        # Fix: add padding before decoding
        padded = data + '=' * (-len(data) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode('utf-8')
        params = json.loads(decoded)
        folder = params["folder"]
        regex = params["regex"]
        full_path = params["full_path"]
    except Exception:
        return JSONResponse({"error": "Invalid data"}, status_code=400)

    normalized_folder = normalize_path(folder)
    normalized_file = Path(normalize_path(full_path))

    try:
        normalized_file.relative_to(normalized_folder)
    except ValueError:
        return JSONResponse({"error": "File outside base folder"}, status_code=403)

    if not normalized_file.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        pattern = re.compile(regex, re.IGNORECASE) if regex else None
    except re.error:
        return JSONResponse({"error": "Invalid regex in view data"}, status_code=400)

    file_list_full = []
    for root, _, filenames in os.walk(normalized_folder):
        for name in filenames:
            fp = os.path.join(root, name)
            if pattern is None or pattern.search(fp):
                file_list_full.append(fp)

    b64_path = base64.urlsafe_b64encode(str(normalized_file).encode()).decode()
    image_url = f"/image/{b64_path}"
    filenames_only = [Path(fp).name for fp in file_list_full]

    return JSONResponse({
        "filename": normalized_file.name,
        "full_path": str(normalized_file),
        "image_url": image_url,
        "file_list": filenames_only,
        "file_list_full": file_list_full,
        "encoded_data": data
    })

@app.get("/view/{data}")
async def view_page(request: Request, data: str):
    return templates.TemplateResponse("view.html", {"request": request})

@app.get("/results/list")
async def list_saved_files():
    try:
        files = [f.name for f in RESULTS_DIR.iterdir() if f.is_file()]
        return JSONResponse(files)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/save-image")
async def save_image(request: Request):
    try:
        data = await request.json()
        src_path = data.get("path")
        if not src_path:
            return JSONResponse({"error": "Missing path"}, status_code=400)

        src = Path(src_path)
        if not src.is_file():
            return JSONResponse({"error": "File not found"}, status_code=404)

        dst = RESULTS_DIR / src.name
        if dst.exists():
            return JSONResponse({"saved": True, "already_exists": True})

        shutil.copy2(src, dst)
        return JSONResponse({"saved": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/regexcheck", response_class=HTMLResponse)
async def regex_check_page(request: Request):
    return templates.TemplateResponse("regexcheck.html", {"request": request})

if __name__ == "__main__":
    local_ips = get_local_ip_addresses()
    print("\n" + "="*80)
    print("📸 Photo Gallery Application")
    print("="*80)
    print(f"• Starting server on http://127.0.0.1:8095")
    for ip in local_ips:
        print(f"• Also available at: http://{ip}:8095")
    print(f"• Cache size: {MAX_CACHE_SIZE_MB} MB (RAM only)")
    print(f"• Batch size: {BATCH_SIZE} images per load")
    print(f"• Save path: {RESULTS_DIR}")
    print("\n🔗 Example URLs:")
    print("  http://127.0.0.1:8095/                     → обычный режим")
    print("  http://127.0.0.1:8095/?save=true           → режим сохранения")
    print("  http://127.0.0.1:8095/?path=C:%5CPhotos&regex=.*%5C.jpg$")
    if local_ips:
        print(f"  http://{local_ips[0]}:8095/                 → с другого устройства в сети")
    print("\n💡 Tips:")
    print("  ┌─ Обычный режим:")
    print("  │   • ЛКМ по превью — открыть в текущей вкладке")
    print("  │   • Колёсико / ПКМ — открыть в новой вкладке")
    print("  │")
    print("  └─ Режим сохранения (?save=true):")
    print("      • ЛКМ по превью — сохранить в папку 'results' (красная рамка)")
    print("      • Колёсико / ПКМ — открыть в новой вкладке")
    print("      • Сохранённые файлы помечены красной рамкой (4px)")
    print("      • Чтобы убрать рамку — удалите файл из 'results' и перезагрузите страницу")
    print("  • Прокрутка до низа + движение колёсика вниз = автоматическая подгрузка")
    print("  • Кнопка 'Load more' — ручная подгрузка следующей партии")
    print("="*80 + "\n")

    parser = argparse.ArgumentParser()
    parser.add_argument("-u")
    args = parser.parse_args()
    if args.u:
        print(args)
        wb.open_new_tab(args.u)

    uvicorn.run(app, host="0.0.0.0", port=8095)