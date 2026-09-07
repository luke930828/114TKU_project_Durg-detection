"""
從爬蟲的 Record volume 串流萃取還原資料庫需要的欄位。

刻意不用 json.load()：images.json 是 1 GB，整份讀進來大約要 3~4 GB 記憶體——
那正是 INFRA-02 把這台機器搞到 OOM 的寫法。這裡用 raw_decode 配一個滑動
緩衝區讀進來，再逐筆寫成 JSONL 出去，兩端都只在記憶體裡放一筆。

（第一版把結果收在一個 dict 裡再 json.dump，輸出 782 MB，
　下游 json.load 立刻被 OOM killer 殺掉。同一個錯誤犯兩次，
　所以這裡連中間結果都不留。）
"""
import json
import os
import sys

DEC = json.JSONDecoder()


def stream_records(path, chunk=4 << 20):
    """逐筆吐出 JSON 陣列裡的物件，記憶體佔用只跟「最大的一筆」有關。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        buf = f.read(chunk)
        i = buf.find("[")
        if i < 0:
            return
        buf = buf[i + 1:]
        while True:
            buf = buf.lstrip().lstrip(",").lstrip()
            if buf.startswith("]") or not buf:
                return
            while True:
                try:
                    obj, end = DEC.raw_decode(buf)
                    break
                except ValueError:
                    more = f.read(chunk)
                    if not more:
                        return          # 檔案被截斷，能救多少算多少
                    buf += more
            yield obj
            buf = buf[end:]


def main():
    # 只有這份小索引留在記憶體：網址 → 時間/來源/關鍵字，給 ai_analysis_results 用。
    index = {}

    # ---- 文字內容 → suspect_websites.html_content ----
    n = 0
    with open("/out/text.jsonl", "w", encoding="utf-8") as out:
        for r in stream_records("/r/nlp_text.json"):
            url = r.get("url")
            if not url:
                continue
            kw = r.get("matched") or []
            index[url] = {"timestamp": r.get("timestamp"),
                          "source": r.get("source"), "keywords": kw}
            out.write(json.dumps({
                "url": url,
                "text_content": (r.get("text_content") or "")[:200000],
                "keywords": kw,
                "timestamp": r.get("timestamp"),
                "source": r.get("source"),
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"nlp_text.json：{n} 筆", flush=True)

    # ---- 商品圖 → suspect_websites.images_data ----
    # 後端的 receive_crawler_raw_data 本來就只存 product_images_b64，
    # screenshot_b64 / full_screenshot_base64 從來沒進過資料庫，
    # 那兩個欄位正是這個檔案 1 GB 的原因。
    n = imgs = 0
    with open("/out/images.jsonl", "w", encoding="utf-8") as out:
        for r in stream_records("/r/images.json"):
            url = r.get("url")
            if not url:
                continue
            pics = []
            for item in (r.get("product_images") or []):
                if isinstance(item, dict):
                    b64 = (item.get("base64_data") or item.get("base64")
                           or item.get("data") or item.get("image"))
                else:
                    b64 = item
                if b64 and isinstance(b64, str):
                    pics.append(b64)
            index.setdefault(url, {"timestamp": r.get("timestamp"),
                                   "source": r.get("source"), "keywords": []})
            n += 1
            if not pics:
                continue
            imgs += len(pics)
            out.write(json.dumps({"url": url, "images": pics,
                                  "timestamp": r.get("timestamp"),
                                  "source": r.get("source")},
                                 ensure_ascii=False) + "\n")
    print(f"images.json：{n} 筆，取出 {imgs} 張商品圖", flush=True)

    with open("/out/index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    for name in ("text.jsonl", "images.jsonl", "index.json"):
        print(f"  {name:14} {os.path.getsize('/out/' + name) / 1e6:8.1f} MB")
    print(f"索引：{len(index)} 個網址")


if __name__ == "__main__":
    sys.exit(main())
