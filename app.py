from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import gspread
from datetime import datetime, timedelta
import threading
import re
import os
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
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)
    spreadsheet = gc.open(SPREADSHEET_NAME)
    sheet = spreadsheet.sheet1
    try:
        attendance_sheet = spreadsheet.worksheet(ATTENDANCE_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        attendance_sheet = spreadsheet.add_worksheet(title=ATTENDANCE_WORKSHEET_NAME, rows="1000", cols="6")
        attendance_sheet.append_row(["التاريخ", "اسم اللاعب", "الحالة", "الطول", "الوزن", "تنبيهات المدرب"])
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
        
        # تصفح شيت اللاعبين لحساب الإحصائيات
        for row in all_records[1:]:
            if len(row) >= 7:
                status = row[6].strip()
                
                # 1. اللاعبين النشطين
                if status == 'Approved':
                    active_players += 1
                    
                    # 4. حساب الاشتراكات المنتهية (العمود التاسع Index 8)
                    end_date_raw = row[8].strip() if len(row) > 8 else ""
                    if end_date_raw and end_date_raw not in ("غير محدد", "---", ""):
                        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
                            try:
                                parsed_date = datetime.strptime(end_date_raw, fmt).date()
                                if parsed_date < today:
                                    expired_subscriptions += 1
                                break
                            except ValueError:
                                continue
                                
                # 3. طلبات التسجيل الجديدة
                elif status == 'Pending':
                    new_requests += 1
                    
        # 2. حساب حضور اليوم من شيت التحضير
        today_attendance = 0
        if attendance_sheet is not None:
            with _sheets_lock:
                attendance_records = attendance_sheet.get_all_values()
            for row in attendance_records[1:]:
                if len(row) >= 3:
                    # التحقق من مطابقة تاريخ اليوم وحالة الحضور
                    if row[0].strip() == today_str and row[2].strip() == 'حاضر':
                        today_attendance += 1
                        
        # تمرير الأرقام الحقيقية المحسوبة إلى ملف الـ HTML
        return render_template('dashboard.html', 
                               active_players=active_players, 
                               today_attendance=today_attendance, 
                               new_requests=new_requests, 
                               expired_subscriptions=expired_subscriptions)
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
            row_data = [new_id, name, birth_year, phone, "", "", status, join_date, "غير محدد"]
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

# 4. مسار جلب اللاعبين المقبولين فقط
def get_age_category(birth_year_str):
    try:
        birth_year = int(str(birth_year_str).strip())
        current_year = datetime.now().year
        age = current_year - birth_year
        if 4 <= age <= 9: return "البراعم"
        elif 10 <= age <= 12: return "الابتدائية"
        elif 13 <= age <= 15: return "المتوسطة"
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

        cell_updates = []
        attendance_rows = []

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

            if height:
                cell_updates.append({'range': f'E{row_idx}', 'values': [[height]]})
            if weight:
                cell_updates.append({'range': f'F{row_idx}', 'values': [[weight]]})

            attendance_rows.append([date, name, status, height, weight, announcement])

        with _sheets_lock:
            if cell_updates:
                sheet.batch_update(cell_updates)
            if attendance_rows:
                attendance_sheet.append_rows(attendance_rows)
            invalidate_cache()

        return jsonify({
            "success": True,
            "message": f"تم حفظ {len(attendance_rows)} سجل ميداني بنجاح! 🏆"
        }), 200
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if 'quota' in msg.lower() or '429' in msg:
            return jsonify({"success": False, "message": "تم تجاوز الحد المسموح به من Google API. حاول بعد دقيقة."}), 429
        return jsonify({"success": False, "message": f"خطأ في Google Sheets: {msg}"}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/')
def home():
    return render_template('attendance.html')
if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('FLASK_PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=debug_mode)