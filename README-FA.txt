FilmBuff Railway Subtitle Engine v2
===================================

این ZIP عمداً بدون پوشه مخفی و بدون فایل‌هایی مثل .github یا .env ساخته شده است.
بعد از Extract، همه فایل‌ها را مستقیم Drag & Drop داخل ریشه Repository گیت‌هاب کن.

فایل‌های ریشه:
- app.py
- requirements.txt
- Dockerfile
- railway.json
- ENVIRONMENT.txt
- README-FA.txt

راه‌اندازی:
1) یک Repository جدید بساز.
2) همین 6 فایل را مستقیم داخل ریشه Repository آپلود و Commit کن.
3) Railway > New Project > Deploy from GitHub Repo.
4) در Service > Variables مقدار FILMBUFF_SUBTITLE_API_KEY را اضافه کن.
5) متغیرهای پیشنهادی ENVIRONMENT.txt را اضافه کن.
6) در Settings > Networking یک Public Domain بساز.
7) آدرس /health را باز کن. باید ok=true و ffmpeg=true و ffprobe=true باشد.

در Cloudflare Worker:
FILMBUFF_SUBTITLE_API_URL=https://YOUR-RAILWAY-DOMAIN
FILMBUFF_SUBTITLE_API_KEY=همان کلید Railway

D1 Binding:
SUBTITLE_DB -> filmbuff-subtitles

نکته:
ویدئو از Railway عبور نمی‌کند. Worker فقط برای MKVهایی که هنوز در D1 Cache نشده‌اند،
از Railway می‌خواهد Track متنی فارسی داخل همان MKV را با FFmpeg به WebVTT تبدیل کند.
