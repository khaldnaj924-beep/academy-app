#!/usr/bin/env bash
#
# deploy.sh — سكربت أتمتة نشر/تحديث تطبيق أكاديمية التسجيل (Flask)
# -------------------------------------------------------------------
# يقوم بـ:
#   1. سحب آخر تحديثات الكود من Git.
#   2. تفعيل البيئة الافتراضية وتثبيت/تحديث الاعتماديات.
#   3. التحقق من سلامة الكود (compile) قبل إعادة التشغيل.
#   4. إعادة تشغيل خدمة Gunicorn بأمان عبر systemd.
#   5. التحقق من أن الخدمة تعمل بعد التحديث.
#   6. إرسال إشعارات تيليجرام: عند بدء النشر، وعند نجاحه أو فشله.
#
# الاستخدام:   ./deploy.sh
# -------------------------------------------------------------------

# أوقف التنفيذ فورًا عند أي خطأ، أو متغير غير معرّف، أو فشل في أنبوب.
set -euo pipefail

# ============= إعدادات قابلة للتعديل =============
APP_DIR="/home/username/myapp"     # مسار مجلد المشروع على الخادم
VENV_DIR="$APP_DIR/venv"           # مسار البيئة الافتراضية
SERVICE_NAME="myapp"               # اسم خدمة systemd
BRANCH="main"                      # فرع Git المراد نشره
APP_MODULE="app"                   # اسم ملف بايثون بدون .py (app.py)
ENV_FILE="$APP_DIR/.env"           # مسار ملف المتغيرات البيئية
# =================================================

# ألوان للإخراج (اختياري، لتوضيح الرسائل)
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $1"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $1"; }
fail() { echo -e "${RED}[deploy] خطأ:${NC} $1" >&2; exit 1; }

# ---------- قراءة التوكن ومعرّف الدردشة من ملف .env ----------
# نقرأ المتغيرات المطلوبة فقط دون تنفيذ الملف كله (أكثر أمانًا).
read_env_var() {
    # $1 = اسم المتغير — يُرجع القيمة بعد إزالة علامات الاقتباس والمسافات.
    grep -E "^[[:space:]]*$1=" "$ENV_FILE" 2>/dev/null \
        | tail -n1 \
        | cut -d= -f2- \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

if [[ -f "$ENV_FILE" ]]; then
    TELEGRAM_BOT_TOKEN="$(read_env_var TELEGRAM_BOT_TOKEN)"
    TELEGRAM_CHAT_ID="$(read_env_var TELEGRAM_CHAT_ID)"
else
    warn "ملف .env غير موجود في $ENV_FILE — سيتم تعطيل إشعارات تيليجرام."
    TELEGRAM_BOT_TOKEN=""
    TELEGRAM_CHAT_ID=""
fi

# ---------- دالة إرسال رسالة تيليجرام عبر curl ----------
# لا توقف النشر إذا فشل الإرسال (الإشعار ثانوي بالنسبة للنشر).
send_telegram() {
    local message="$1"
    if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
        warn "توكن أو معرّف دردشة تيليجرام غير متوفر — تم تخطي الإشعار."
        return 0
    fi
    curl -s --max-time 10 \
        -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode="HTML" \
        --data-urlencode text="${message}" \
        >/dev/null 2>&1 \
        || warn "تعذّر إرسال إشعار تيليجرام."
}

# اسم الخادم ووقت التنفيذ لإثراء الرسائل
HOSTNAME_STR="$(hostname 2>/dev/null || echo 'unknown-host')"
START_TIME="$(date '+%Y-%m-%d %H:%M:%S')"

# ---------- مصيدة (trap): ترسل إشعار الفشل تلقائيًا عند أي خطأ ----------
# تُطلق عند أي خروج غير صفري بفضل set -e، فتغطي كل خطوات النشر.
on_error() {
    local exit_code=$?
    send_telegram "❌ <b>فشل النشر</b>
🖥️ الخادم: <code>${HOSTNAME_STR}</code>
🌿 الفرع: <code>${BRANCH}</code>
⏱️ ${START_TIME}
⚠️ رمز الخطأ: <code>${exit_code}</code>"
}
trap on_error ERR

# ===================== بدء النشر =====================
send_telegram "🚀 <b>بدء عملية النشر</b>
🖥️ الخادم: <code>${HOSTNAME_STR}</code>
🌿 الفرع: <code>${BRANCH}</code>
⏱️ ${START_TIME}"

# 0) تأكد أننا في مجلد المشروع
cd "$APP_DIR" || fail "لا يمكن الدخول إلى مجلد المشروع: $APP_DIR"

# 1) سحب آخر التحديثات من Git
log "سحب آخر التحديثات من الفرع '$BRANCH'..."
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"   # يضمن مطابقة الخادم للفرع البعيد تمامًا

# 2) تفعيل البيئة الافتراضية وتثبيت الاعتماديات
log "تفعيل البيئة الافتراضية وتثبيت الاعتماديات..."
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# 3) فحص سلامة الكود قبل إعادة التشغيل (يمنع نشر كود مكسور)
log "التحقق من سلامة الكود..."
python -m py_compile "${APP_MODULE}.py" || fail "الكود يحتوي أخطاء بناء — تم إيقاف النشر."

# 4) إعادة تشغيل الخدمة (Gunicorn عبر systemd)
log "إعادة تشغيل خدمة '$SERVICE_NAME'..."
sudo systemctl restart "$SERVICE_NAME"

# 5) التحقق من أن الخدمة تعمل بعد التحديث
sleep 2
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    log "✅ تم النشر بنجاح، والخدمة تعمل."
else
    warn "الخدمة ليست نشطة! اعرض السجلات بـ:"
    warn "  sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
    fail "فشل النشر — الخدمة غير نشطة بعد إعادة التشغيل."
fi

# ---------- إشعار النجاح ----------
# نزيل مصيدة الخطأ قبل الخروج الطبيعي حتى لا تُطلق عن طريق الخطأ.
trap - ERR
END_TIME="$(date '+%Y-%m-%d %H:%M:%S')"
send_telegram "✅ <b>نجح النشر</b>
🖥️ الخادم: <code>${HOSTNAME_STR}</code>
🌿 الفرع: <code>${BRANCH}</code>
⏱️ ${END_TIME}
🟢 الخدمة <code>${SERVICE_NAME}</code> تعمل."

log "اكتمل النشر."
