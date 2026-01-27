import os
import re
import base64
import json
import asyncio
import shutil
from pathlib import Path
from mimetypes import guess_type
from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from io import BytesIO
from PIL import Image
from collections import OrderedDict

# --- Настройки ---
MAX_CACHE_SIZE_MB = 1024
MAX_CACHE_SIZE_BYTES = MAX_CACHE_SIZE_MB * 1024 * 1024
BATCH_SIZE = 150

BASE_DIR = Path(__file__).parent.resolve()
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# RAM caches
image_content_cache = OrderedDict()
preview_cache = OrderedDict()


def get_local_ip_addresses() -> list[str]:
    """
    Возвращает список локальных IPv4-адресов машины (без 127.0.0.1 и loopback).
    Используется для отображения всех доступных URL при запуске сервера на 0.0.0.0.
    """
    import socket
    ips = set()

    # Метод 1: через подключение к внешнему адресу (надёжно определяет основной LAN-IP)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass

    # Метод 2: через getaddrinfo хостнейма
    try:
        hostname = socket.gethostname()
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        for info in addr_info:
            ip = info[4][0]
            if ip != "127.0.0.1" and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass

    # Метод 3: перебор интерфейсов (работает не везде, но попробуем)
    try:
        from socket import AF_INET, SOCK_DGRAM
        import uuid
        # Этот метод менее надёжен, но иногда даёт доп. адреса
        # Пропускаем — основные два метода обычно достаточно
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
        media_type, content = get_full_image(norm_path)
        return 'image/jpeg', content

    preview_cache[norm_path] = ('image/jpeg', content, asyncio.get_event_loop().time())
    evict_old_items(preview_cache, MAX_CACHE_SIZE_BYTES // 2)
    return 'image/jpeg', content

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    default_folder = r"C:\Users\admin19\Downloads"
    default_regex = r".*\.(png|jpg|jpeg|PNG|JPG|JPEG)$"
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
    normalized_folder = normalize_path(folder)
    if not os.path.isdir(normalized_folder):
        return {"error": "Directory not found"}

    try:
        pattern = re.compile(regex, re.IGNORECASE) if regex else None
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}

    files = []
    for root, _, filenames in os.walk(normalized_folder):
        for name in filenames:
            if pattern is None or pattern.search(name):
                full_path = os.path.join(root, name)
                files.append(full_path)

    files.sort()
    total = len(files)
    batch = files[offset:offset + limit]
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
        path = base64.urlsafe_b64decode(b64_path).decode('utf-8')
        norm_path = normalize_path(path)
        media_type, content = get_full_image(norm_path)
        return Response(content=content, media_type=media_type)
    except Exception:
        return HTMLResponse("Image not available", status_code=404)

@app.get("/preview/{b64_path}")
async def serve_preview(b64_path: str):
    try:
        path = base64.urlsafe_b64decode(b64_path).decode('utf-8')
        norm_path = normalize_path(path)
        media_type, content = get_preview_image(norm_path)
        return Response(content=content, media_type=media_type)
    except Exception:
        return HTMLResponse("Preview not available", status_code=404)

@app.get("/api/view")
async def api_view(data: str = Query(...)):
    try:
        decoded = base64.urlsafe_b64decode(data).decode('utf-8')
        params = json.loads(decoded)
        folder = params["folder"]
        regex = params["regex"]
        filename = params["filename"]
    except Exception:
        return JSONResponse({"error": "Invalid data"}, status_code=400)

    if '/' in filename or '\\' in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    normalized_folder = normalize_path(folder)
    if not os.path.isdir(normalized_folder):
        return JSONResponse({"error": "Folder not found"}, status_code=400)

    try:
        pattern = re.compile(regex, re.IGNORECASE) if regex else None
    except re.error:
        return JSONResponse({"error": "Invalid regex in view data"}, status_code=400)

    file_list = []
    target_file = None
    for root, _, filenames in os.walk(normalized_folder):
        for name in filenames:
            if pattern is None or pattern.search(name):
                full_path = os.path.join(root, name)
                file_list.append(full_path)
                if Path(full_path).name.lower() == filename.lower():
                    target_file = full_path

    if not target_file or not os.path.isfile(target_file):
        return JSONResponse({"error": "File not found in filtered list"}, status_code=404)

    b64_path = base64.urlsafe_b64encode(target_file.encode()).decode()
    image_url = f"/image/{b64_path}"
    filenames_only = [Path(fp).name for fp in file_list]

    return JSONResponse({
        "filename": Path(target_file).name,
        "full_path": target_file,
        "image_url": image_url,
        "file_list": filenames_only,
        "encoded_data": data
    })

@app.get("/view/{data}")
async def view_page(request: Request, data: str):
    return templates.TemplateResponse("view.html", {"request": request})

# === SAVE MODE ЭНДПОИНТЫ (ТОЛЬКО ПО ТЗ) ===

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

# === ЗАПУСК ===
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
    
    uvicorn.run(app, host="0.0.0.0", port=8095)