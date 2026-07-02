"""
backfill_attendance_ids.py — أداة لمرة واحدة

تُعبّئ عمود «معرف اللاعب» (العمود G) في ورقة التحضير للسجلات التي لا تحمل معرّفاً بعد،
عن طريق مطابقة اسم اللاعب مع الشيت الرئيسي. الأسماء المكرّرة (أكثر من معرّف) تُتخطّى
حفاظاً على الدقة، وكذلك الأسماء التي لا تطابق أي لاعب.

الاستخدام:
    python backfill_attendance_ids.py            # معاينة فقط (لا يكتب شيئاً)
    python backfill_attendance_ids.py --apply     # تنفيذ الكتابة فعلياً

يعيد استخدام اتصال Google Sheets ودوال المساعدة من app.py.
"""
import sys
import app


def build_name_to_ids(main_values):
    """خريطة: اسم اللاعب -> قائمة المعرّفات (لكشف التكرار)."""
    name_to_ids = {}
    for r in main_values[1:]:
        if len(r) >= 2:
            pid = r[0].strip()
            nm = r[1].strip()
            if pid and nm:
                ids = name_to_ids.setdefault(nm, [])
                if pid not in ids:
                    ids.append(pid)
    return name_to_ids


def main():
    apply = '--apply' in sys.argv

    if app.sheet is None or app.attendance_sheet is None:
        print("❌ لا يوجد اتصال بقاعدة البيانات. تحقق من ملف الاعتماد.")
        return

    main_values = app.sheet.get_all_values()
    att_values = app.attendance_sheet.get_all_values()
    name_to_ids = build_name_to_ids(main_values)

    updates = []
    already = filled = skipped_ambiguous = skipped_nomatch = 0

    for i, r in enumerate(att_values[1:], start=2):
        current_id = r[6].strip() if len(r) > 6 else ""
        if current_id:
            already += 1
            continue

        name = r[1].strip() if len(r) > 1 else ""
        ids = name_to_ids.get(name, [])

        if len(ids) == 1:
            updates.append({'range': f'G{i}', 'values': [[ids[0]]]})
            filled += 1
            print(f"  ✅ صف {i}: «{name}» -> معرّف {ids[0]}")
        elif len(ids) > 1:
            skipped_ambiguous += 1
            print(f"  ⚠️  صف {i}: الاسم «{name}» مكرّر ({len(ids)} معرّفات) — تخطّي.")
        else:
            skipped_nomatch += 1
            print(f"  ⚠️  صف {i}: لا يوجد لاعب بالاسم «{name}» — تخطّي.")

    print("\n" + "=" * 48)
    print(f"للتعبئة: {filled} | موجود مسبقاً: {already} | "
          f"مكرّر: {skipped_ambiguous} | بدون تطابق: {skipped_nomatch}")

    if not updates:
        print("لا يوجد ما يُعبّأ. ✅")
        return

    if apply:
        app.attendance_sheet.batch_update(updates)
        app.invalidate_cache()
        print(f"✅ تم تحديث {len(updates)} صفاً في ورقة التحضير.")
    else:
        print("👀 معاينة فقط — لم تُكتب أي تغييرات. أعد التشغيل مع --apply للتنفيذ.")


if __name__ == '__main__':
    main()
