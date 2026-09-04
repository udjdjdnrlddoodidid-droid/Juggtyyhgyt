FilmBuff Railway Subtitle Engine v3 (Async Jobs)
================================================

این نسخه مشکل 524 را با حذف درخواست طولانی حل می‌کند.
Railway فوراً job_id برمی‌گرداند و FFmpeg در پس‌زمینه ادامه می‌دهد.
Worker هر چند ثانیه وضعیت را می‌پرسد و بعد از آماده شدن VTT آن را در D1 ذخیره می‌کند.

فایل‌ها همگی در ریشه هستند و پوشه مخفی وجود ندارد.

بعد از Deploy:
/health
باید ok=true, mode=async-jobs, ffmpeg=true, ffprobe=true بدهد.

Secret مشترک:
FILMBUFF_SUBTITLE_API_KEY

نکته: مسیر /extract فقط برای تست دستی باقی مانده و Worker از آن استفاده نمی‌کند.
