from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import gspread
from datetime import datetime, timedelta
import threading
import re
import os
import requests
from dotenv import load_dotenv

# 🔐 تحميل متغيرات البيئة من ملف .env (إن وُجد)
load_dotenv()

app = Flask(__name__)

# 🔐 قراءة الأسرار والإعدادات من متغيرات البيئة بدلاً من كتابتها داخل الكود
GOOGLE_CREDENTIALS_PATH = os.environ.get('GOOGLE_KEY_PATH', 'key.json')
SPREADSHEET_NAME = os.environ.get('SPREADSHEET_NAME', 'تسجيل الاكاديمية')
ATTENDANCE_WORKSHEET_NAME = os.environ.get('ATTENDANCE_WORKSHEET_NAME', 'التحضير')

# 🛡️ L3 #8: تقييد CORS إلى نطاقات محددة فقط (في الإنتاج، عدّل القائمة)
# تُقرأ النطاقات المسموح بها من متغير البيئة CORS_ORIGINS (مفصولة بفواصل)
_cors_origins_raw = os.environ.get(
    'CORS_ORIGINS',
    'http://localhost:5000,http://127.0.0.1:5000'
)
_cors_origins = [o.strip() for o in _cors_origins_raw.split(',') if o.strip()]
CORS(app, resources={r"/*": {"origins": _cors_origins}})

# 🛡️ L3 #1: قفل للعمليات الحساسة على Google Sheets لمنع race conditions
_sheets_lock = threading.Lock()

# 🛡️ L3 #2: دالة تنظيف المدخلات قبل أي كتابة لقاعدة البيانات
_HTML_TAG = re.compile(r'<[^>]*>')
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')

def sanitize_input(value, max_len=200):
    if value is None:
        return ''
    s = str(value).strip()
    s = _HTML_TAG.sub('', s)
    s = _CONTROL_CHARS.sub('', s)
    if s and s[0] in ('=', '+', '-', '@'):
        s = "'" + s
    return s[:max_len]

def validate_phone(phone):
    if not phone: return False
    cleaned = re.sub(r'[\s\-]', '', phone)
    return bool(re.fullmatch(r'05\d{8}', cleaned))

def validate_birth_year(year_str):
    try:
        y = int(str(year_str).strip())
        return 1990 <= y <= datetime.now().year
    except (ValueError, TypeError):
        return False

sheet = None
attendance_sheet = None

# الاتصال بجوجل شيت
try:
    import json as _json
    _creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if _creds_json:
        _creds_dict = _json.loads(_creds_json)
        from google.oauth2.service_account import Credentials as _Creds
        _scopes = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        _creds = _Creds.from_service_account_info(_creds_dict, scopes=_scopes)
        gc = gspread.authorize(_creds)
    else:
        gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)
    spreadsheet = gc.open(SPREADSHEET_NAME)
    sheet = spreadsheet.sheet1
    try:
        attendance_sheet = spreadsheet.worksheet(ATTENDANCE_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        attendance_sheet = spreadsheet.add_worksheet(title=ATTENDANCE_WORKSHEET_NAME, rows="1000", cols="6")
        attendance_sheet.append_row(["التاريخ", "اسم اللاعب", "الحالة", "الطول", "الوزن", "تنبيهات المدرب", "معرف اللاعب"])
except Exception as e:
    print(f"Error connecting to Google Sheets: {e}")

def require_sheet():
    if sheet is None:
        return jsonify({"success": False, "message": f"لا يوجد اتصال بقاعدة البيانات. تحقق من ملف الاعتماد ({GOOGLE_CREDENTIALS_PATH})"}), 503
    return None

# 🛡️ L3 #3: cache بسيط في الذاكرة لتقليل ضربات Google Sheets API
_cache_lock = threading.Lock()
_cache = {'sheet_values': None, 'sheet_ts': 0}
_CACHE_TTL = 30

def get_sheet_values_cached():
    import time
    now = time.time()
    with _cache_lock:
        if _cache['sheet_values'] is not None and (now - _cache['sheet_ts']) < _CACHE_TTL:
            return _cache['sheet_values']
        values = sheet.get_all_values()
        _cache['sheet_values'] = values
        _cache['sheet_ts'] = now
        return values

def invalidate_cache():
    with _cache_lock:
        _cache['sheet_values'] = None
        _cache['sheet_ts'] = 0

# مسار عرض لوحة الإدارة وحساب الإحصائيات الديناميكية
@app.route('/')
@app.route('/dashboard')
def view_dashboard():
    err = require_sheet()
    if err: return err
    try:
        all_records = get_sheet_values_cached()
        
        today = datetime.now().date()
        today_str = today.strftime("%Y-%m-%d")
        
        active_players = 0
        new_requests = 0
        expired_subscriptions = 0
        pending_list = []
        approved_list = []

        # 2. حساب حضور اليوم من شيت التحضير (خريطة: معرّف اللاعب -> الحالة)
        today_attendance_map = {}
        if attendance_sheet is not None:
            with _sheets_lock:
                attendance_records = attendance_sheet.get_all_values()
            for arow in attendance_records[1:]:
                if len(arow) >= 7 and arow[0].strip() == today_str:
                    pid = arow[6].strip()
                    st = arow[2].strip()
                    if pid and st in ('حاضر', 'غائب'):
                        today_attendance_map[pid] = st
        today_attendance = sum(1 for v in today_attendance_map.values() if v == 'حاضر')

        # تصفح شيت اللاعبين لحساب الإحصائيات والقوائم
        for idx, row in enumerate(all_records[1:], start=2):
            if len(row) >= 7:
                status = row[6].strip()

                # 1. اللاعبين النشطين
                if status == 'Approved':
                    active_players += 1
                    name = row[1] if len(row) > 1 else ""
                    player_id = row[0].strip() if row and row[0] else ""

                    # استخراج سنة الميلاد لتحديد الفئة العمرية
                    dob_raw = row[2] if len(row) > 2 else ""
                    birth_year = ""
                    if dob_raw:
                        parts = dob_raw.replace("-", "/").split("/")
                        if len(parts) == 3:
                            birth_year = parts[0] if len(parts[0]) == 4 else parts[2]
                        elif len(parts) == 1:
                            birth_year = parts[0]

                    # 4. حالة الاشتراك (العمود التاسع Index 8)
                    end_date_raw = row[8].strip() if len(row) > 8 else ""
                    sub_status = "unknown"
                    end_display = "غير محدد"
                    if end_date_raw and end_date_raw not in ("غير محدد", "---", ""):
                        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
                            try:
                                parsed_date = datetime.strptime(end_date_raw, fmt).date()
                                end_display = parsed_date.strftime("%Y-%m-%d")
                                if parsed_date < today:
                                    sub_status = "expired"
                                    expired_subscriptions += 1
                                else:
                                    sub_status = "active"
                                break
                            except ValueError:
                                continue

                    # قائمة اللاعبين المقبولين (تُستخدم في تبويبي التحضير والاشتراكات)
                    approved_list.append({
                        "row_index": idx,
                        "player_id": player_id,
                        "name": name,
                        "category": get_age_category(birth_year),
                        "today_status": today_attendance_map.get(player_id, ""),
                        "end_date": end_display,
                        "sub_status": sub_status
                    })

                # 3. طلبات التسجيل الجديدة
                elif status == 'Pending':
                    new_requests += 1
                    pending_list.append({
                        "row_index": idx,
                        "name": row[1] if len(row) > 1 else "",
                        "phone": row[3] if len(row) > 3 else "",
                        "birth_year": row[2] if len(row) > 2 else ""
                    })

        # تمرير الأرقام الحقيقية المحسوبة إلى ملف الـ HTML
        return render_template('index.html',
                               active_players=active_players, 
                               today_attendance=today_attendance, 
                               new_requests=new_requests,
                               expired_subscriptions=expired_subscriptions,
                               pending_list=pending_list,
                               approved_list=approved_list)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# 1. مسار تسجيل لاعب جديد
@app.route('/register', methods=['GET', 'POST'])
def register_player():
    if request.method == 'GET':
        return render_template('register.html')
    err = require_sheet()
    if err: return err
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "لم يتم إرسال بيانات"}), 400

        name = sanitize_input(data.get('name', ''), max_len=80)
        phone = sanitize_input(data.get('phone', ''), max_len=20)
        birth_year = sanitize_input(data.get('birth_year', ''), max_len=4)

        if not name or len(name) < 3:
            return jsonify({"success": False, "message": "الاسم قصير جداً (3 أحرف على الأقل)"}), 400
        if not validate_phone(phone):
            return jsonify({"success": False, "message": "رقم الهاتف غير صالح (يجب أن يكون 05XXXXXXXX)"}), 400
        if not validate_birth_year(birth_year):
            return jsonify({"success": False, "message": "سنة الميلاد غير صالحة"}), 400

        with _sheets_lock:
            all_records = sheet.get_all_values()
            existing_ids = []
            for r in all_records[1:]:
                if r and r[0].strip().isdigit():
                    existing_ids.append(int(r[0]))
            new_id = (max(existing_ids) + 1) if existing_ids else 1

            status = 'Pending'
            join_date = datetime.now().strftime("%Y-%m-%d")
            row_data = [new_id, name, birth_year, phone, "", "", status, join_date, ""]
            sheet.append_row(row_data)
            invalidate_cache()

        return jsonify({"success": True, "message": "تم إرسال الطلب بنجاح!"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# 2. مسار جلب الطلبات المعلقة
@app.route('/pending_requests', methods=['GET'])
def get_pending_requests():
    err = require_sheet()
    if err: return err
    try:
        all_records = get_sheet_values_cached()
        if len(all_records) <= 1: return jsonify([])
        pending_players = []
        for idx, row in enumerate(all_records[1:], start=2):
            if len(row) >= 7 and row[6] == 'Pending':
                pending_players.append({"row_index": idx, "name": row[1], "phone": row[3]})
        return jsonify(pending_players), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# 3. مسار القبول أو الرفض
@app.route('/update_player_status', methods=['POST'])
def update_player_status():
    err = require_sheet()
    if err: return err
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "البيانات المُرسلة غير صالحة أو فارغة"}), 400
        row_idx = data.get('row_index')
        new_status = data.get('status')
        if not row_idx or new_status not in ('Approved', 'Rejected'):
            return jsonify({"success": False, "message": "بيانات غير صالحة"}), 400

        with _sheets_lock:
            try:
                current_status = sheet.cell(int(row_idx), 7).value
            except Exception:
                return jsonify({"success": False, "message": "الصف غير موجود في الشيت"}), 404
            if current_status == new_status:
                return jsonify({"success": True, "message": "الحالة محدّثة مسبقاً"}), 200
            sheet.update_cell(int(row_idx), 7, new_status)
            invalidate_cache()
        return jsonify({"success": True, "message": "تم تحديث حالة اللاعب بنجاح"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# 3.1 مسار تسجيل حضور/غياب لاعب واحد فورياً عند الضغط على الزر
@app.route('/mark_attendance', methods=['POST'])
def mark_attendance():
    err = require_sheet()
    if err: return err
    if attendance_sheet is None:
        return jsonify({"success": False, "message": "ورقة التحضير غير متاحة"}), 503
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "البيانات غير صالحة"}), 400

        status = sanitize_input(data.get('status', ''), max_len=10)
        if status not in ('حاضر', 'غائب'):
            return jsonify({"success": False, "message": "حالة غير صالحة"}), 400

        # 🆔 نعتمد على معرّف اللاعب الفريد (العمود الأول) بدلاً من الاسم لتفادي تكرار الأسماء
        player_id = sanitize_input(data.get('player_id', ''), max_len=20)
        if not player_id:
            return jsonify({"success": False, "message": "معرّف اللاعب مفقود"}), 400

        # جلب اسم اللاعب من الشيت الرئيسي عبر معرّف اللاعب (لا نثق باسم يرسله العميل)
        all_records = get_sheet_values_cached()
        name = ""
        for r in all_records[1:]:
            if r and r[0].strip() == player_id:
                name = (r[1].strip() if len(r) > 1 else "")
                break
        if not name:
            return jsonify({"success": False, "message": "لا يوجد لاعب بهذا المعرّف"}), 404

        today_str = datetime.now().strftime("%Y-%m-%d")

        with _sheets_lock:
            # تحديث سجل اليوم لهذا المعرّف إن وُجد، وإلا إضافة سجل جديد (لتفادي التكرار عند التبديل)
            att_values = attendance_sheet.get_all_values()
            found_row = None
            for i, r in enumerate(att_values[1:], start=2):
                if len(r) >= 7 and r[0].strip() == today_str and r[6].strip() == player_id:
                    found_row = i
                    break
            if found_row:
                attendance_sheet.update_cell(found_row, 3, status)
            else:
                # العمود السابع (G) يخزّن معرّف اللاعب للمطابقة المستقبلية
                attendance_sheet.append_row([today_str, name, status, "", "", "", player_id])
            invalidate_cache()

        return jsonify({"success": True, "message": f"تم تسجيل «{status}» لـ {name}"}), 200
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if 'quota' in msg.lower() or '429' in msg:
            return jsonify({"success": False, "message": "تم تجاوز الحد المسموح به من Google API. حاول بعد دقيقة."}), 429
        return jsonify({"success": False, "message": f"خطأ في Google Sheets: {msg}"}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# 4. مسار جلب اللاعبين المقبولين فقط
def get_age_category(birth_year_str):
    try:
        birth_year = int(str(birth_year_str).strip())
        current_year = datetime.now().year
        age = current_year - birth_year
        if 4 <= age <= 9: return "البراعم"
        elif 10 <= age <= 12: return "الابتدائية"
        elif 13 <= age <= 15: return "المتوسطة"
        elif age >= 16: return "الشباب"
        else: return "غير محدد"
    except (ValueError, TypeError):
        return "غير محدد"

@app.route('/dashboard_data', methods=['GET'])
def get_dashboard_data():
    err = require_sheet()
    if err: return err
    try:
        all_records = get_sheet_values_cached()
        if len(all_records) <= 1: return jsonify([])
        approved_players = []
        for idx, row in enumerate(all_records[1:], start=2):
            if len(row) >= 7 and row[6] == 'Approved':
                dob_raw = row[2] if len(row) > 2 else ""
                birth_year = ""
                if dob_raw:
                    parts = dob_raw.replace("-", "/").split("/")
                    if len(parts) == 3:
                        birth_year = parts[0] if len(parts[0]) == 4 else parts[2]
                    elif len(parts) == 1:
                        birth_year = parts[0]
                category = get_age_category(birth_year)
                approved_players.append({
                    "row_index": idx,
                    "name": row[1],
                    "height": row[4] if len(row) > 4 else "",
                    "weight": row[5] if len(row) > 5 else "",
                    "join_date": row[7] if len(row) > 7 else "",
                    "category": category
                })
        return jsonify(approved_players), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# 5. مسار تجديد الاشتراك
@app.route('/renew_subscription', methods=['POST'])
def renew_subscription():
    err = require_sheet()
    if err: return err
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "البيانات غير صالحة"}), 400
        row_idx_raw = data.get('row_index')
        player_name = sanitize_input(data.get('name', ''), max_len=80)

        try:
            row_idx = int(row_idx_raw)
            if row_idx < 2: raise ValueError
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "معرّف الصف غير صالح"}), 400

        with _sheets_lock:
            try:
                row_values = sheet.row_values(row_idx)
            except Exception:
                return jsonify({"success": False, "message": "الصف غير موجود في الشيت"}), 404
            if not row_values or len(row_values) < 7:
                return jsonify({"success": False, "message": "بيانات الصف غير مكتملة"}), 404

            current_end_raw = row_values[8] if len(row_values) > 8 else ""

            today = datetime.now().date()
            new_start = today
            if current_end_raw and current_end_raw not in ("غير محدد", "---", ""):
                parsed = None
                for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
                    try:
                        parsed = datetime.strptime(str(current_end_raw).strip(), fmt).date()
                        break
                    except (ValueError, TypeError):
                        continue
                if parsed:
                    min_reasonable = today - timedelta(days=365 * 5)
                    max_reasonable = today + timedelta(days=365 * 5)
                    if min_reasonable <= parsed <= max_reasonable and parsed > today:
                        new_start = parsed

            new_end_date = new_start + timedelta(days=30)
            new_end_str  = new_end_date.strftime("%Y-%m-%d")
            new_join_str = today.strftime("%Y-%m-%d")

            sheet.batch_update([
                {'range': f'G{row_idx}', 'values': [['Approved']]},
                {'range': f'H{row_idx}', 'values': [[new_join_str]]},
                {'range': f'I{row_idx}', 'values': [[new_end_str]]},
            ])
            invalidate_cache()

        return jsonify({
            "success": True,
            "message": f"تم تجديد اشتراك {player_name} حتى {new_end_str} ✅",
            "new_join_date": new_join_str,
            "new_end_date": new_end_str
        }), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

def trigger_n8n_notification(player_name, phone_number, status, date, notif_type=None, message=None):
    """
    يرسل طلب لـ n8n webhook فور حفظ تحضير لاعب، عشان يرسل رسالة واتساب فورية
    لولي الأمر بدون انتظار جدولة زمنية.
    تُستخدم أيضاً للإشعارات العامة (الحالة = "إشعار") مع نوع الإشعار ونص الرسالة.
    """
    try:
        webhook_url = "https://n8n.roboualain.site/webhook/absence-notification"
        payload = {
            "اسم اللاعب": player_name,
            "رقم الواتساب": phone_number,
            "الحالة": status,
            "التاريخ": date
        }
        if notif_type is not None:
            payload["نوع الإشعار"] = notif_type
        if message is not None:
            payload["الرسالة"] = message
        requests.post(webhook_url, json=payload, timeout=5)
        return True
    except Exception as e:
        # لا نوقف حفظ التحضير حتى لو فشل الإشعار
        print(f"فشل إرسال إشعار n8n: {e}")
        return False

# 6. مسار حفظ التحضير والقياسات
@app.route('/save_dashboard', methods=['POST'])
def save_dashboard():
    err = require_sheet()
    if err: return err
    if attendance_sheet is None:
        return jsonify({"success": False, "message": "ورقة التحضير غير متاحة"}), 503
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "البيانات غير صالحة"}), 400

        date = sanitize_input(data.get('date', ''), max_len=20)
        announcement = sanitize_input(data.get('announcement', ''), max_len=500)
        players = data.get('players', [])

        if not date:
            return jsonify({"success": False, "message": "تاريخ الحصة مطلوب"}), 400
        if not isinstance(players, list) or len(players) == 0:
            return jsonify({"success": False, "message": "لا يوجد لاعبون لحفظهم"}), 400

        cell_updates = []   # تحديثات الطول/الوزن على الشيت الرئيسي
        att_updates = []    # تحديث سجلات تحضير موجودة (نفس التاريخ + معرّف اللاعب)
        att_appends = []    # سجلات تحضير جديدة
        saved_count = 0
        notify_list = []    # اللاعبون المحفوظون بنجاح لإشعار n8n الفوري

        # قيم الشيت الرئيسي لاستخراج معرّف اللاعب (العمود الأول) حسب رقم الصف
        main_values = get_sheet_values_cached()

        with _sheets_lock:
            # فهرسة سجلات التحضير الحالية حسب (التاريخ + معرّف اللاعب) لتفادي التكرار
            att_values = attendance_sheet.get_all_values()
            att_index = {}
            for i, r in enumerate(att_values[1:], start=2):
                if len(r) >= 7 and r[6].strip():
                    att_index[(r[0].strip(), r[6].strip())] = i

            for p in players:
                try:
                    row_idx = int(p.get('row_index', 0))
                    if row_idx < 2: continue
                except (ValueError, TypeError):
                    continue

                name   = sanitize_input(p.get('name', ''), max_len=80)
                height = sanitize_input(p.get('height', ''), max_len=10)
                weight = sanitize_input(p.get('weight', ''), max_len=10)
                status = sanitize_input(p.get('status', 'غائب'), max_len=10)

                if status not in ('حاضر', 'غائب'):
                    status = 'غائب'

                # 🆔 معرّف اللاعب من العمود الأول للشيت الرئيسي (المطابقة بالمعرّف لا بالاسم)
                player_id = ""
                if 0 < row_idx <= len(main_values):
                    mrow = main_values[row_idx - 1]
                    player_id = mrow[0].strip() if mrow and mrow[0] else ""
                if not player_id:
                    continue  # بدون معرّف فريد لا نحفظ للحفاظ على الاتساق

                if height:
                    cell_updates.append({'range': f'E{row_idx}', 'values': [[height]]})
                if weight:
                    cell_updates.append({'range': f'F{row_idx}', 'values': [[weight]]})

                existing = att_index.get((date, player_id))
                if existing:
                    # تحديث سجل نفس اليوم بدل إضافة صف مكرّر (B..G)
                    att_updates.append({
                        'range': f'B{existing}:G{existing}',
                        'values': [[name, status, height, weight, announcement, player_id]]
                    })
                else:
                    # العمود السابع (G) يخزّن معرّف اللاعب
                    att_appends.append([date, name, status, height, weight, announcement, player_id])
                saved_count += 1

                # 📱 رقم واتساب ولي الأمر من العمود الرابع (D) بالشيت الرئيسي
                phone = mrow[3].strip() if len(mrow) > 3 and mrow[3] else ""
                notify_list.append({'name': name, 'phone': phone, 'status': status})

            if cell_updates:
                sheet.batch_update(cell_updates)
            if att_updates:
                attendance_sheet.batch_update(att_updates)
            if att_appends:
                attendance_sheet.append_rows(att_appends)
            invalidate_cache()

        # 🔔 إشعار n8n الفوري لكل لاعب تم حفظ تحضيره بنجاح (خيط منفصل حتى لا تتأخر الاستجابة)
        if notify_list:
            def _notify_n8n(items, date_snapshot):
                for it in items:
                    trigger_n8n_notification(it['name'], it['phone'], it['status'], date_snapshot)
            try:
                threading.Thread(target=_notify_n8n, args=(notify_list, date), daemon=True).start()
            except Exception as ex:
                print(f"[n8n Thread] خطأ: {ex}")

        return jsonify({
            "success": True,
            "message": f"تم إرسال {saved_count} سجل ميداني بنجاح! 🏆"
        }), 200
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if 'quota' in msg.lower() or '429' in msg:
            return jsonify({"success": False, "message": "تم تجاوز الحد المسموح به من Google API. حاول بعد دقيقة."}), 429
        return jsonify({"success": False, "message": f"خطأ في Google Sheets: {msg}"}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/send_notification', methods=['POST'])
def send_notification():
    err = require_sheet()
    if err: return err
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "البيانات غير صالحة"}), 400

        notif_type = sanitize_input(data.get('type', ''), max_len=50)
        target = sanitize_input(data.get('target', 'الكل'), max_len=50)
        message = sanitize_input(data.get('message', ''), max_len=500)

        today_str = datetime.now().strftime("%Y-%m-%d")

        # 🔔 إشعار n8n بالإشعار العام (خيط منفصل حتى لا يبطئ الاستجابة)
        # التوزيع على أولياء الأمور يتولاه workflow الـ general-notification في n8n
        def _notify_general(nt, tg, msg, dt):
            try:
                requests.post(
                    "https://n8n.roboualain.site/webhook/general-notification",
                    json={
                        "نوع_الإشعار": nt,
                        "الفئة": tg,
                        "الملاحظة": msg,
                        "التاريخ": dt
                    },
                    timeout=8
                )
            except Exception as ex:
                print(f"فشل إرسال إشعار n8n العام: {ex}")

        try:
            threading.Thread(target=_notify_general,
                             args=(notif_type, target, message, today_str),
                             daemon=True).start()
        except Exception as ex:
            print(f"[n8n Thread] خطأ: {ex}")

        return jsonify({"success": True, "message": "تم إرسال الإشعار بنجاح ✅"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/attendance')
def home():
    return render_template('attendance.html')
if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('FLASK_PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=debug_mode)