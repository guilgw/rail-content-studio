"""把当前时间线写成剪映可编辑草稿，并打一个换位置后仍能找到素材的压缩包。

剪映专业版 6.0 及以后、Mac 版 11.x 读取 draft_info.json。素材 path 通常是绝对路径，
草稿文件夹一旦搬家就会丢失。剪映自己的可移植写法是把草稿目录换成
`##_draftpath_placeholder_<draft_id>_##`，打开时再替换成草稿的实际位置。
压缩包使用这种占位符；写进本机草稿目录的那一份保留绝对路径，方便马上打开。
"""
import json
import os
import shutil
import uuid
import zipfile

import roughcut_export
import pipeline

README = """剪映工程说明
==============

这个文件夹是一条可编辑的粗剪草稿，不是已经烧好字幕的视频。

轨道
----
- 画面：视频轨，原声已静音
- 配音：音频轨
- 背景音乐：空的音频轨，可在剪映里自行放入音乐
- 字幕、花字：文本轨，是可编辑文字，不是印在画面上的图片

在这台电脑上打开
----------------
如果导出时已经写入剪映草稿目录，打开剪映专业版，在草稿列表中找到同名工程。
剪映当时已经开着的话，先完整退出再打开。

换一台电脑，或列表里没有这个草稿
--------------------------------
1. 打开剪映的草稿目录。Mac 常见位置：
   ~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft/
2. 把整个文件夹放进该目录，不要只复制 draft_info.json。
3. 重新打开剪映，在草稿列表中打开。

素材路径
--------
压缩包里的素材路径使用剪映的占位符：
##_draftpath_placeholder_<草稿编号>_##
剪映专业版 6.0 及以后、Mac 版 11.x 打开草稿时，会把它换成这个文件夹的实际位置。
因此 materials/ 必须和 draft_info.json 留在同一个文件夹里。

如果仍然提示素材丢失，在剪映里对丢失的片段选择重新链接，指向本文件夹 materials/ 中的同名文件。
"""


def portable_prefix(draft_id):
    return f"##_draftpath_placeholder_{draft_id}_##"


def apply_swaps(result, swaps):
    """沿用粗剪导出的镜头校验，并让被替换的素材进入剪映工程。"""
    prepared = roughcut_export.prepare(result, swaps or {})
    clips = {c["id"]: c for c in pipeline.load("library.json")["clips"]}
    for meta_id, meta in (result.get("clips") or {}).items():
        clips.setdefault(meta_id, {"id": meta_id, **meta})
    for seg in prepared["segments"]:
        for shot in seg["shots"]:
            clip = clips.get(shot["clip"])
            if not clip or not clip.get("file"):
                raise ValueError("有镜头找不到素材文件，请重新制作后再导出。")
            shot["title"] = clip.get("title") or shot.get("title") or shot["clip"]
    prepared["_clip_list"] = list(clips.values())
    return prepared


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, path)


def _rewrite(node, source, target):
    if isinstance(node, dict):
        return {key: _rewrite(value, source, target) for key, value in node.items()}
    if isinstance(node, list):
        return [_rewrite(value, source, target) for value in node]
    if isinstance(node, str) and source and source in node:
        return node.replace(source, target)
    return node


def _add_bgm_track(info):
    if any(track.get("name") == "背景音乐" for track in info.get("tracks") or []):
        return info
    voice = next((track for track in info["tracks"] if track.get("name") == "配音"), None)
    if not voice:
        return info
    bgm = json.loads(json.dumps(voice))
    bgm["id"] = uuid.uuid4().hex
    bgm["name"] = "背景音乐"
    bgm["segments"] = []
    index = info["tracks"].index(voice) + 1
    info["tracks"].insert(index, bgm)
    return info


def finalize_draft(draft_dir, total_s, portable, source_dir=None):
    """补上背景音乐轨，并按是否打包决定素材用绝对路径还是剪映占位符。"""
    info_path = os.path.join(draft_dir, "draft_info.json")
    meta_path = os.path.join(draft_dir, "draft_meta_info.json")
    info = _add_bgm_track(_load_json(info_path))
    meta = _load_json(meta_path) if os.path.isfile(meta_path) else {}
    draft_id = meta.get("draft_id") or str(uuid.uuid4()).upper()
    absolute = os.path.abspath(source_dir or draft_dir)
    meta.update(draft_id=draft_id, draft_name=os.path.basename(draft_dir),
                draft_root_path=os.path.dirname(absolute), tm_duration=int(round(total_s * 1_000_000)))
    if portable:
        prefix = portable_prefix(draft_id)
        info = _rewrite(info, absolute, prefix)
        meta["draft_fold_path"] = prefix
    else:
        meta["draft_fold_path"] = absolute
    _dump_json(info_path, info)
    _dump_json(meta_path, meta)
    readme = os.path.join(draft_dir, "README.txt")
    if not os.path.isfile(readme):
        with open(readme, "w", encoding="utf-8") as handle:
            handle.write(README)
    return info


def track_counts(info):
    names = {"画面": "video", "配音": "audio", "字幕": "subtitle", "花字": "sticker", "背景音乐": "bgm"}
    counts = {key: 0 for key in names.values()}
    for track in info.get("tracks") or []:
        key = names.get(track.get("name"))
        if key:
            counts[key] = len(track.get("segments") or [])
    return counts


def _zip_draft(draft_dir, out_dir):
    exports = os.path.join(out_dir, "exports")
    os.makedirs(exports, exist_ok=True)
    name = "jianying-" + uuid.uuid4().hex + ".zip"
    target = os.path.join(exports, name)
    root_name = os.path.basename(draft_dir)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for folder, _dirs, files in os.walk(draft_dir):
            for filename in files:
                full = os.path.join(folder, filename)
                archive.write(full, os.path.join(root_name, os.path.relpath(full, draft_dir)))
    return "/exports/" + name, name


def export_project(result, swaps, draft_root, out_dir, install=True):
    if result.get("preview_only"):
        raise ValueError("当前是随仓库附带的演示，还没有本机配音和草稿。请先在脚本工作室选用一版并完成制作。")
    prepared = apply_swaps(result, swaps)
    name = pipeline.draft_name_for(prepared.get("title") or result.get("title"))
    os.makedirs(draft_root, exist_ok=True)
    draft_dir, _shots, _stickers = pipeline.write_draft(
        prepared["segments"], prepared["skill"], prepared["_clip_list"], draft_root, name)
    info = finalize_draft(draft_dir, float(prepared["total_s"]), portable=False)
    staging = os.path.join(out_dir, "jianying-portable")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    portable_dir = os.path.join(staging, name)
    shutil.copytree(draft_dir, portable_dir)
    finalize_draft(portable_dir, float(prepared["total_s"]), portable=True, source_dir=draft_dir)
    url, filename = _zip_draft(portable_dir, out_dir)
    installed = bool(install)
    return {"name": name, "path": os.path.abspath(draft_dir), "installed": installed,
            "tracks": track_counts(info), "download": url, "filename": filename,
            "hint": "打开剪映 → 草稿"}
