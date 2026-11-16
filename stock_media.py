"""按句子补齐素材：先看本地库，匹配不足时再从 Pixabay 下载竖屏或高清视频。

密钥只从 .env 或环境变量读取，不写入页面、日志或异常信息。
下载只接受 cdn.pixabay.com 的 HTTPS 地址，并限制体积。
"""
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

import llm

API = "https://pixabay.com/api/videos/"
DOWNLOAD_HOST = "cdn.pixabay.com"
PAGE_HOST = "pixabay.com"
LICENSE = "https://pixabay.com/service/license-summary/"
MAX_BYTES = 40 * 1024 * 1024
MAX_LIBRARY = 200
LOW_COVERAGE = 0.34
NOTICE = "Pixabay 素材发布前请人工核查画面中的人物和商标。"
MISSING = "未配置 Pixabay 密钥，只使用本地素材。"
# 这些词描述行程本身。主题是选题里更具体的意象，例如蜜蜂，而不是火车。
GENERIC = {
    "app", "cabin", "city", "countryside", "depart", "euston", "highland", "journey", "lake",
    "loch", "london", "morning", "mountain", "night", "phone", "platform", "rail", "railway",
    "scotland", "sleeper", "station", "ticket", "track", "train", "travel", "window",
}
ZH_THEME = (("小蜜蜂", "bee"), ("蜜蜂", "bee"), ("蜂巢", "bee"), ("蜂蜜", "bee"), ("蝴蝶", "butterfly"), ("马特洪", "matterhorn"), ("阿尔卑斯", "alps"))
EN_THEME = {"bee": "bee", "bees": "bee", "honey": "bee", "honeybee": "bee", "hive": "bee",
            "butterfly": "butterfly", "matterhorn": "matterhorn", "alps": "alps", "alpine": "alps"}


def configured():
    return bool(_secret())


def _secret():
    return (llm.load_env().get("PIXABAY_API_KEY") or "").strip()


def labels(values, limit=8):
    out = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())[:40]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def visual_theme(title, lines=None):
    """从选题和脚本里抽出整条片子的视觉主题。普通行程词不算主题。"""
    found = []

    def add(token):
        token = EN_THEME.get(token, token)
        if token and token not in GENERIC and token not in found:
            found.append(token)

    text = title or ""
    for zh, token in ZH_THEME:
        if zh in text:
            add(token)
    for word in re.findall(r"[A-Za-z]+", text):
        mapped = EN_THEME.get(word.lower())
        if mapped:
            add(mapped)
    for line in lines or []:
        english = (line.get("en") or "").lower()
        for word, token in EN_THEME.items():
            if re.search(rf"\b{re.escape(word)}\b", english):
                add(token)
        for tag in line.get("tags") or []:
            if isinstance(tag, str) and tag.lower() not in GENERIC:
                add(tag.lower())
    return found[:3]


# 这些是行程里的功能性画面。已经有主题镜头时，不必再为它们单独下载“蜜蜂看手机”。
PLAIN = {"app", "city", "depart", "euston", "highland", "journey", "loch", "london", "morning", "night",
         "phone", "platform", "rail", "railway", "scotland", "sleeper", "station", "ticket", "track", "train"}


def lacks_theme_shot(clips, theme, need):
    """还没有主题画面，或这句有具体场景、但没有同时带主题和该场景的素材。"""
    if not theme:
        return False
    themed = [clip for clip in clips if set(theme) & set(clip.get("tags") or [])]
    if not themed:
        return True
    # 乡村、车窗、车厢这类场景要和主题一起出现；票券、手机则复用已有主题镜头。
    scene = [tag for tag in need or [] if tag not in PLAIN and tag not in theme]
    if not scene:
        return False
    return not any(set(scene) & set(clip.get("tags") or []) for clip in themed)


def english_query(text, tags=None):
    """把中文画面需求改写成英文检索词。没有脚本模型时退回已有英文标签。"""
    tags = labels(tags, 6)
    fallback = " ".join(tags[:4]) or "train travel"
    plain = " ".join(str(text or "").split())[:300]
    if not re.search(r"[\u4e00-\u9fff]", plain):
        return (plain or fallback)[:80]
    if not llm.model_config("script").get("api_key"):
        return fallback[:80]
    try:
        data, _ = llm.script_json(
            "把中文画面需求改写成视频素材检索用的英文关键词。只返回 JSON：{\"query\":\"2到6个英文单词\"}。不要品牌、价格和优惠码。",
            json.dumps({"中文": plain, "已有标签": tags}, ensure_ascii=False), 0.2)
        query = re.sub(r"[^A-Za-z0-9 ,'-]", " ", str((data or {}).get("query") or ""))
        query = " ".join(query.split())[:80]
        return query or fallback[:80]
    except Exception:
        return fallback[:80]


def clean_query(text):
    plain = " ".join(str(text or "").split())
    if not plain or len(plain) > 80:
        raise ValueError("请输入 80 字以内的搜索词。")
    if re.search(r"[\u0000-\u001f]", plain):
        raise ValueError("搜索词包含无法使用的字符。")
    return plain


def _api_url(params):
    secret = _secret()
    if not secret:
        raise ValueError(MISSING)
    query = urllib.parse.urlencode({**params, "key": secret, "safesearch": "true"})
    return API + "?" + query


def _read_json(response):
    data = response.read(1024 * 1024)
    if len(data) >= 1024 * 1024:
        raise ValueError("在线素材搜索结果过大。")
    return json.loads(data.decode("utf-8"))


def _public_failure(exc, fallback):
    text = str(exc)
    if isinstance(exc, ValueError) and text in (
            MISSING, "在线素材搜索结果过大。", "在线素材搜索结果无法读取。", "素材地址不在允许的下载域名内。",
            "素材下载跳转到了允许范围之外。", "素材超过 40 MB，已取消下载。", "下载内容不是可用的视频文件。",
            "没有找到可下载的竖屏或高清素材。", "素材编号无效。", "在线素材已达到 200 段上限，请先清理 output/stock 后再下载。",
            "请输入 80 字以内的搜索词。", "搜索词包含无法使用的字符。") and "key" not in text.lower():
        return exc
    return ValueError(fallback)


def fetch_hits(params, opener=urllib.request.urlopen):
    if not configured():
        raise ValueError(MISSING)
    try:
        request = urllib.request.Request(_api_url(params), headers={"User-Agent": "rail-content-studio"})
        with opener(request, timeout=30) as response:
            payload = _read_json(response)
    except Exception as exc:
        raise _public_failure(exc, "在线素材搜索失败，请稍后重试。") from None
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        raise ValueError("在线素材搜索结果无法读取。")
    return hits


def _variant_ok(variant):
    if not isinstance(variant, dict):
        return False
    try:
        width, height = int(variant.get("width") or 0), int(variant.get("height") or 0)
        size = int(variant.get("size") or 0)
    except (TypeError, ValueError):
        return False
    url = str(variant.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != DOWNLOAD_HOST or parsed.username or parsed.port not in (None, 443):
        return False
    if "key=" in urllib.parse.unquote(parsed.query).lower():
        return False
    if width < 240 or height < 240 or (size and size > MAX_BYTES):
        return False
    return True


def best_variant(videos):
    """竖屏优先；480p 以上的竖屏排在横屏高清之前，过小的竖屏让位给高清横屏。"""
    found = [item for item in (videos or {}).values() if _variant_ok(item)] if isinstance(videos, dict) else []
    if not found:
        return None

    def score(item):
        short = min(item["width"], item["height"])
        portrait = item["height"] >= item["width"] and short >= 480
        return (1 if portrait else 0, 1 if short >= 720 else 0, short, item["width"] * item["height"])

    return max(found, key=score)


def _public_url(url, host):
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != host or "key=" in urllib.parse.unquote(parsed.query).lower():
        return ""
    return urllib.parse.urlunparse(parsed)


def describe(raw):
    variant = best_variant(raw.get("videos") if isinstance(raw, dict) else None)
    if not raw or not variant or not isinstance(raw.get("id"), int):
        return None
    tags = str(raw.get("tags") or "").strip()
    height, width = int(variant["height"]), int(variant["width"])
    return {"id": raw["id"], "title": (tags or "Pixabay 视频")[:80], "duration": max(1, int(raw.get("duration") or 1)),
            "width": width, "height": height, "author": str(raw.get("user") or "")[:80],
            "page": _public_url(raw.get("pageURL"), PAGE_HOST) or LICENSE, "thumbnail": _public_url(variant.get("thumbnail"), DOWNLOAD_HOST),
            "portrait": height >= width, "hd": min(width, height) >= 720, "provider": "Pixabay", "license": LICENSE}


def search(text, opener=urllib.request.urlopen):
    query = english_query(clean_query(text))
    hits = [item for item in (describe(raw) for raw in fetch_hits(
        {"q": query, "video_type": "film", "per_page": 20, "order": "popular"}, opener)) if item]
    hits.sort(key=lambda item: (item["portrait"] and item["hd"], item["portrait"], item["hd"], min(item["width"], item["height"])), reverse=True)
    return {"query": query, "configured": True, "notice": NOTICE, "hits": hits[:12]}


def _ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _download(url, opener):
    parsed = urllib.parse.urlparse(url)
    if not _variant_ok({"url": url, "width": 480, "height": 480, "size": 1}):
        raise ValueError("素材地址不在允许的下载域名内。")
    if parsed.hostname != DOWNLOAD_HOST:
        raise ValueError("素材地址不在允许的下载域名内。")

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlparse(newurl)
            if target.scheme != "https" or target.hostname != DOWNLOAD_HOST:
                raise ValueError("素材下载跳转到了允许范围之外。")
            return urllib.request.Request(newurl, headers={"User-Agent": "rail-content-studio"})

    downloader = opener
    if opener is urllib.request.urlopen:
        downloader = urllib.request.build_opener(Stay).open
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "rail-content-studio"})
        with downloader(request, timeout=60) as response:
            final = response.geturl() if hasattr(response, "geturl") else url
            if urllib.parse.urlparse(final).hostname != DOWNLOAD_HOST:
                raise ValueError("素材下载跳转到了允许范围之外。")
            length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if length and int(length) > MAX_BYTES:
                raise ValueError("素材超过 40 MB，已取消下载。")
            chunks, total = [], 0
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_BYTES:
                    raise ValueError("素材超过 40 MB，已取消下载。")
                chunks.append(block)
    except Exception as exc:
        raise _public_failure(exc, "素材下载失败，请稍后重试。") from None
    data = b"".join(chunks)
    if len(data) < 1024 or b"ftyp" not in data[:64]:
        raise ValueError("下载内容不是可用的视频文件。")
    return data


def _thumbnail(video, dest, thumb_url, opener):
    if thumb_url:
        try:
            data = _download_small(thumb_url, opener)
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                with open(dest, "wb") as handle:
                    handle.write(data)
                return True
        except Exception:
            pass
    exe = _ffmpeg()
    if not exe:
        return False
    try:
        subprocess.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.2", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=480:-2", str(dest)], check=True, timeout=30,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.isfile(dest) and os.path.getsize(dest) > 0


def _download_small(url, opener):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != DOWNLOAD_HOST:
        raise ValueError("缩略图地址不在允许的下载域名内。")
    request = urllib.request.Request(url, headers={"User-Agent": "rail-content-studio"})
    with opener(request, timeout=20) as response:
        final = response.geturl() if hasattr(response, "geturl") else url
        if urllib.parse.urlparse(final).hostname != DOWNLOAD_HOST:
            raise ValueError("缩略图地址不在允许的下载域名内。")
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("缩略图超过大小限制。")
    return data


def _probe(path):
    exe = _ffmpeg()
    if not exe:
        return None
    try:
        result = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not found:
        return None
    return int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3))


def library_path(out):
    return os.path.join(out, "stock-library.json")


def extra_clips(out):
    path = library_path(out)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    clips = payload.get("clips") if isinstance(payload, dict) else None
    return [clip for clip in clips or [] if isinstance(clip, dict) and str(clip.get("id", "")).startswith("px")]


def _save_extra(out, clips):
    path = library_path(out)
    os.makedirs(out, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"clips": clips[-MAX_LIBRARY:]}, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def import_video(root, out, video_id, tags, places, opener=urllib.request.urlopen):
    if not isinstance(video_id, int) or not 0 < video_id < 10**12:
        raise ValueError("素材编号无效。")
    existing = next((clip for clip in extra_clips(out) if clip["id"] == f"px{video_id}"), None)
    if existing and os.path.isfile(os.path.join(root, existing["file"])):
        return public_clip(existing, existing.get("duration") or 1)
    raw = next((item for item in fetch_hits({"id": str(video_id)}, opener) if item.get("id") == video_id), None)
    chosen = best_variant(raw.get("videos") if raw else None)
    if not chosen:
        raise ValueError("没有找到可下载的竖屏或高清素材。")
    if len(extra_clips(out)) >= MAX_LIBRARY:
        raise ValueError("在线素材已达到 200 段上限，请先清理 output/stock 后再下载。")
    folder = os.path.join(out, "stock")
    os.makedirs(folder, exist_ok=True)
    video = os.path.join(folder, f"px{video_id}.mp4")
    temporary = video + ".part"
    try:
        with open(temporary, "wb") as handle:
            handle.write(_download(chosen["url"], opener))
        os.replace(temporary, video)
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
    thumb = os.path.join(folder, f"px{video_id}.jpg")
    has_thumb = _thumbnail(video, thumb, _public_url(chosen.get("thumbnail"), DOWNLOAD_HOST), opener)
    duration = _probe(video) or max(1, int(raw.get("duration") or 1))
    # 标签只采用 Pixabay 对这段视频的描述，不把句子的画面需求写进去。
    tag_list = labels(re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,24}", str(raw.get("tags") or "").lower()))
    place_list = labels(places, 6)
    record = {"id": f"px{video_id}", "file": f"output/stock/px{video_id}.mp4", "title": (str(raw.get("tags") or "Pixabay 视频").strip() or "Pixabay 视频")[:80],
              "tags": tag_list, "location": "、".join(place_list) or "Pixabay", "quality": 4 if min(chosen["width"], chosen["height"]) >= 720 else 3,
              "issues": [], "pexels": _public_url(raw.get("pageURL"), PAGE_HOST) or LICENSE, "places": place_list, "author": str(raw.get("user") or "")[:80],
              "license": LICENSE, "provider": "Pixabay", "page": _public_url(raw.get("pageURL"), PAGE_HOST) or LICENSE,
              "thumb": f"output/stock/px{video_id}.jpg" if has_thumb else "", "stock": True, "duration": round(float(duration), 2)}
    saved = [clip for clip in extra_clips(out) if clip["id"] != record["id"]]
    saved.append(record)
    _save_extra(out, saved)
    return public_clip(record, record["duration"])


def public_clip(record, duration):
    return {"id": record["id"], "title": record["title"], "file": record["file"], "tags": record.get("tags") or [],
            "location": record.get("location") or "", "places": record.get("places") or [], "quality": record.get("quality") or 3,
            "issues": record.get("issues") or [], "pexels": record.get("page") or record.get("pexels") or LICENSE,
            "clip_s": round(float(duration), 2), "provider": "Pixabay", "author": record.get("author") or "",
            "license": record.get("license") or LICENSE, "thumb": record.get("thumb") or "", "stock": True,
            "page": record.get("page") or LICENSE}


def remember_in_result(out, clip):
    path = os.path.join(out, "result.json")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        result = json.load(handle)
    result.setdefault("clips", {})[clip["id"]] = clip
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _weak(line, clips, durations, retrieve, max_shot_s):
    _need, places, top, _bad = retrieve(line, clips, durations)
    need = labels(line.get("tags") or _need, 6)
    if not top or float(top[0].get("coverage") or 0) < LOW_COVERAGE:
        return True, need, labels(places if line.get("places") is None else line.get("places"), 6)
    n_need = int(min(3, max(1, -(-float(line.get("seg_s") or 1) // max_shot_s))))
    usable = [item for item in top if not item.get("issues")]
    return len(usable) < n_need and float(top[0].get("coverage") or 0) < 0.8, need, labels(places if line.get("places") is None else line.get("places"), 6)


def fill_gaps(lines, clips, durations, retrieve, root, out, max_shot_s=3.0, search_fn=None, importer=None, theme=None):
    """本地匹配度低，或还没有主题画面时，自动下载一条 Pixabay 视频。失败时保留本地结果。"""
    clips, durations = list(clips), dict(durations)
    theme = visual_theme("", []) if theme is None else list(theme)
    if not configured() and importer is None:
        return clips, durations, {"configured": False, "imported": [], "theme": theme, "message": MISSING}
    search_fn = search_fn or (lambda text: search(text)["hits"])
    importer = importer or (lambda video_id, tags, places: import_video(root, out, video_id, tags, places))
    imported, seen = [], set()
    for line in lines:
        if len(imported) >= 6:
            break
        try:
            weak, need, places = _weak(line, clips, durations, retrieve, max_shot_s)
        except Exception:
            continue
        if not weak and not lacks_theme_shot(clips, theme, need):
            continue
        query = english_query(" ".join([line.get("zh") or "", *theme, *need]).strip(), [*theme, *need])
        for token in theme:
            if token not in query.split():
                query = f"{token} {query}".strip()
        if query in seen:
            continue
        seen.add(query)
        try:
            hits = search_fn(query)
            hit = next((item for item in hits if item.get("portrait") or item.get("hd")), None) or (hits[0] if hits else None)
            if not hit:
                continue
            # 不把本句需求传给入库标签；Pixabay 自己的 tags 才是这段视频的内容。
            clip = importer(int(hit["id"]), [], places)
            if not line.get("tags"):
                line["tags"] = labels(clip.get("tags") or need)
        except Exception:
            continue
        if any(item["id"] == clip["id"] for item in clips):
            imported.append({"id": clip["id"], "title": clip["title"], "author": clip.get("author") or "", "page": clip.get("page") or LICENSE, "license": LICENSE})
            continue
        clips.append({"id": clip["id"], "file": clip["file"], "title": clip["title"], "tags": list(clip.get("tags") or []),
                      "location": clip.get("location") or "", "quality": clip.get("quality") or 3, "issues": [],
                      "pexels": clip.get("pexels") or LICENSE, "places": clip.get("places") or [], "author": clip.get("author") or "",
                      "license": LICENSE, "provider": "Pixabay", "page": clip.get("page") or LICENSE, "thumb": clip.get("thumb") or "",
                      "stock": True})
        durations[clip["id"]] = float(clip.get("clip_s") or hit.get("duration") or 1)
        imported.append({"id": clip["id"], "title": clip["title"], "author": clip.get("author") or "", "page": clip.get("page") or LICENSE, "license": LICENSE})
    if imported:
        subject = "、".join(theme)
        message = f"已按主题「{subject}」补充 {len(imported)} 段素材。" if subject else f"本地匹配不足的 {len(imported)} 句已自动选用 Pixabay 素材。"
        message += NOTICE
    else:
        message = "已检查在线素材，没有需要自动下载的新镜头。" if configured() or importer else MISSING
    return clips, durations, {"configured": configured(), "imported": imported, "theme": theme, "message": message}
