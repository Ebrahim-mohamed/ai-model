from string import Template

# Bucket B — Prompt Templates (verbatim scripts). Every string here is
# returned to the user EXACTLY as written — the LLM never rephrases,
# summarizes, or re-authors these (claude.md's architecture override,
# confirmed by the user). Source: real, literal Arabic text extracted
# directly from D:\project\project\data\{سياق المكالمة.pdf,
# ارسال الايميلات التواصل.pdf, العيادات التخصييه.pdf, التوجيهات.pdf,
# Scripts.xlsx (sheet "Instruction" — WhatsApp open/close scripts,
# authoritative over the واتساب سكربت.png screenshot of the same table)}
# and the real "Complaint Handling" sheet inside
# D:\project\project\data\Raylab-Knowledgebase-V1.xlsx — cross-referenced
# against the bucket/template_id design in D:\project\structure.pdf.pdf.
# No fabricated content — every literal sentence below is a direct
# transcription of real source text. $variables mark the only points the
# LLM is allowed to fill in (names, brand, numbers).

#### Call Script — سياق المكالمة.pdf ####
# claude.md's constitution: these four lines are reused verbatim across
# every call-based flow (general call center, and — per structure.pdf.pdf
# — reused again for Specialized Clinics and Home Visit closings).

call_greeting = Template(
    "صباح / مساء الخير $brand للاشعه والتحاليل $employee_name مع حضرتك"
)

call_customer_address = Template(
    "أتشرف بــ الاسم ؟ أهالً وسهالً بحضرتك يا $title / $customer_name"
)

call_empathy = Template(
    "ألف سالمة علي حضرتك يا $title / $customer_name"
)

call_closing = Template(
    "شكرا إلتصالك بمراكز $brand"
)

#### Specialized Clinics — العيادات التخصييه.pdf ####
# Section 1 (Call Transfer Script): reuses the greeting/empathy/closing
# templates above, plus this transfer-specific sequence. Real extension
# numbers: 198 (Cairoscan), 196 (Technoscan).

clinics_response = Template(
    "اهلا بيك يا $title / $customer_name اتفضل اقدر اساعدك ازاي"
)

clinics_transfer_announcement = Template(
    "يتم تحويل العميل على قسم العيادات واخطاره انه سيتم ارجاعه للقائمه الرئيسيه "
    "ثم الضغط على رقم 3 للعيادات التخصصيه $extension داخلي $brand"
)

clinics_extra_help = Template(
    "يتم سوال العميل عن اي استفسار اخر"
)

clinics_closing = Template(
    "شكرا لاختيارك كايروسكان - تكنوسكان ، جاري التحويل"
)

#### Home Visit — closing (structure.pdf.pdf §1 "Services Catalogue", Home Visit) ####
# Real literal closing line, reused verbatim (same mechanism as the
# clinics_closing above — same sentence, different trigger context).

home_visit_closing = Template(
    "شكرا الختيارك كايروسكان، جاري التحويل"
)

#### Directives — التوجيهات.pdf — refuse_report_alt_number ####
# The one Bucket B template in the Directives file: the exact, real
# refusal script when a patient asks for their report sent to a
# different number than the one registered on their booking.

refuse_report_alt_number = Template(
    "حفاظا على خصوصية عملائنا، يتم إرسال التقرير فقط على الرقم المسجل بالحجز. "
    "في حال الحاجة إلى نسخة من التقرير، يرجى التوجه إلى الفرع واستالمه بإيصال "
    "الدفع أو بطاقة المريض."
)

#### WhatsApp Script — closing message (الرسالة الختامية لغلق الشات) ####
# Real, verbatim source: D:\project\project\data\Scripts.xlsx, sheet
# "Instruction", row D5 (Cairoscan) / D9 (Technoscan) — real spreadsheet
# text, not OCR off واتساب سكربت.png (that screenshot is the same content
# rendered as a table image; the .xlsx is the authoritative, unambiguous
# source for the exact characters, so it wins over the low-resolution
# screenshot per claude.md's "target schema is law" sourcing discipline).
# Previous version of these two templates was a bare survey URL with a
# transcription error in each link (a missing "e/" path segment and a
# dropped "K" for Cairoscan; "pilx" instead of "plix" for Technoscan) —
# both corrected here against the real cell values.

whatsapp_close_chat_technoscan = Template("\n".join([
    "يرجي العلم انه في حاله عدم الرد سيتم اغلاق المحادثه تلقائيا وفي حاله "
    "الارسال مره اخري سيتم بدء محادثه جديده",
    "شكرا لأختيارك تكنوسكان",
    "لتقييم الخدمه المقدمه من خلال الواتس اب يرجي الضغط علي اللينك ادناه",
    "We would like to know your feedback about your experience with us, "
    "https://prod.plix.co/0y7iLnv",
]))

whatsapp_close_chat_cairoscan = Template("\n".join([
    "يرجي العلم انه في حاله عدم الرد سيتم اغلاق المحادثه تلقائيا وفي حاله "
    "الارسال مره اخري سيتم بدء محادثه جديده",
    "شكرا لأختيارك كايروسكان",
    "لتقييم الخدمه المقدمه من خلال الواتس اب يرجي الضغط علي اللينك ادناه",
    "https://docs.google.com/forms/d/e/1FAIpQLSf2MSTYzvk8ba2tMdL2odMdaavcxqdE5dLcjhtseQYqkKTR6A/viewform",
]))

#### E-mail Templates — ارسال الايميلات التواصل.pdf ####
# Five real, field-structured templates. "Structural accuracy matters more
# than fluency" here (per structure.pdf.pdf's own rationale for these) —
# every field name below is the real field name from the source file, in
# the real order. The LLM fills in $variables only; it never rewrites the
# field labels or reorders them, or downstream email routing breaks.

email_contact_request = Template("\n".join([
    "إلى: $branch_group",
    "نسخة: T-telecall Supervisor",
    "الموضوع: طلب تواصل فرع ($branch_name)",
    "",
    "الادارة المنوطة بالتواصل: $branch_name",
    "اسم العميل: $customer_name",
    "رقم التليفون: $phone_number",
    "سبب طلب التواصل من العميل: $contact_reason",
    "الاستجابة لطلب التواصل: (تُكتب من قبل الفرع)",
    "الامضاء: $agent_name",
    "الوظيفة/الشركة: Call Center Agent",
]))

email_complaint = Template("\n".join([
    "إلى: قسم الشكاوي الخاص بكايروسكان أو تكنوسكان حسب المكالمة",
    "نسخة: T-telecall Supervisor",
    "الموضوع: طلب شكوى ($complaint_type) فرع ($branch_name)",
    "",
    "تاريخ الشكوى: $complaint_date",
    "تاريخ الفحص: $exam_date",
    "اسم المريض: $patient_name",
    "الرقم الذي تم الاتصال منه: $calling_number",
    "رقم الهاتف: $phone_number",
    "الفحص: $exam_name",
    "كود المريض: $patient_code",
    "الفرع: $branch_name",
    "اسم الطبيب المعالج / المستشفى: $doctor_name",
    "رقم الطبيب المعالج / المستشفى: $doctor_phone",
    "محتوى الشكوى: $complaint_details",
    "امضاء الموظف: $agent_name",
    "الوظيفة/الشركة: Call Center Agent",
]))

email_holter_reservation = Template("\n".join([
    "إلى: $branch_group",
    "نسخة: T-telecall Supervisor",
    "الموضوع: طلب حجز هولتر فرع ($branch_name) ($request_date)",
    "",
    "الاسم: $customer_name",
    "تاريخ إرسال طلب التواصل: $request_date",
    "ملاحظات: $holter_notes",
    "رقم الهاتف: $phone_number",
    "تاريخ الحجز: $booking_date",
    "العنوان: $customer_area",
    "الموضوع: طلب حجز هولتر ($hours) ساعة",
    "الامضاء: $agent_name",
    "الوظيفة/الشركة: Call Center Agent",
]))

email_urgent_lab_report = Template("\n".join([
    "إلى: $branch_group + Lab Medical Secretary",
    "نسخة: T-telecall Supervisor",
    "الموضوع: طلب استعجال نتيجة التحاليل الطبية فرع ($branch_name)",
    "",
    "الاسم: $customer_name",
    "رقم الهاتف: $phone_number",
    "الكود (Accession Number): $accession_number",
    "التحاليل: $test_names",
    "الفرع: $branch_name",
    "وقت سحب العينة (Registration Date): $registration_date",
    "وقت ظهور التحاليل (Result Date): $result_date",
    "ملاحظات: $notes",
    "رد السكرتارية: (تُكتب من قبل السكرتارية الطبية بالمعامل)",
    "امضاء الموظف: $agent_name",
]))

email_thanks = Template("\n".join([
    "إلى: $branch_group",
    "نسخة: T-telecall Supervisor",
    "الموضوع: ميل شكر فرع ($branch_name)",
    "",
    "اسم العميل: $customer_name",
    "رقم التليفون: $phone_number",
    "الفرع: $branch_name",
    "الجهة المنوطة بالشكر: $thanked_staff",
    "سبب الشكر: $thanks_reason",
    "الامضاء: $agent_name",
    "الوظيفة/الشركة: Call Center Agent",
]))

#### Complaint Handling — Opening Sentences (Bucket B half) ####
# Real, verbatim opening line per complaint type — extracted directly
# from the real "Complaint Handling" sheet (13 real rows found; the
# structure.pdf.pdf plan estimated ~12). Injected as the AI's first
# response the moment the complaint type is classified (claude.md:
# metadata.complaint_type is the exact-match routing key — never a
# free-text search).

complaint_open_branch_or_doctor_misconduct = Template(
    "أنا آسف جدًا على ما تعرّضتم له، وأقدّر تواصلكم معنا. من فضلكم أخبروني "
    "بتفاصيل الموقف حتى نتمكن من متابعته بجدية"
)

complaint_open_customer_service_misconduct = Template(
    "أنا آسف على ما حدث خلال مكالمتكم السابقة. من فضلكم أخبروني بتاريخ "
    "المكالمة والرقم الذي اتصلتم منه حتى نتابع الأمر."
)

complaint_open_results_delay = Template(
    "أعتذر عن التأخير. من فضلكم أعطوني بياناتكم حتى أتابع الأمر فورًا"
)

# NOTE (real data-quality issue, preserved as-is per claude.md's "trust
# the contract, never repair" rule): the source sheet has the SAME text
# pasted into all four content columns for this complaint type — opening,
# resolution steps, escalation condition, and closing are identical in
# the real data. Not a transcription error on our side.
complaint_open_contract_issue = Template(
    "1- \tيتم التأكد من التعاقد الخاص بالعميل اي هو نوع التعاقد بالتحديد \n"
    "2- \tيتم الرد علي استفسار العميل إذا كان (مده التحويل – اذا كان في "
    "امكانيه لاحضار التحويل او الموافقه من خلال الفرع ام لا )\n"
    "3- \tاذا كان العميل يشتكي من السعر يجب مراجعه نوع الفحص جيدا و ادخاله "
    "علي السيستم و و ابلاغ العميل به و الاستفسار منه عن السعر الذي تم ابلاغه به "
)

complaint_open_lucky_card_issue = Template(
    "من فضلكم أعطوني رقم الهاتف المسجّل في النظام حتى أراجع بياناتكم."
)

complaint_open_appointment_mismatch = Template(
    "أعتذر عن هذا الإرباك. من فضلكم أعطوني بياناتكم الكاملة حتى أراجع الحجز الآن."
)

complaint_open_wrong_booking = Template(
    "أعتذر عن هذا الخطأ. سأراجع تفاصيل حجزكم معكم الآن للتأكد من الفحص الصحيح."
)

complaint_open_report_error = Template(
    "أعتذر على هذا الأمر وأقدر تواصلكم معنا. من فضلكم أخبروني بطبيعة الخطأ "
    "بالتفصيل حتى نتابعه بجدية."
)

complaint_open_exam_delay_at_branch = Template(
    "أعتذر على انتظاركم. من فضلكم أخبروني بتفاصيل الموقف حتى أتابع الأمر مع الفرع."
)

complaint_open_wrong_results_received = Template(
    "أعتذر على هذا الخطأ. من فضلكم أعطوني بياناتكم الكاملة حتى أراجع الأمر فورًا."
)

complaint_open_pricing_error = Template(
    "أعتذر على هذا الإرباك. من فضلكم أخبروني بنوع الفحوصات التي أجريتموها "
    "والسعر الذي أُبلغتم به حتى أراجع الأمر."
)

complaint_open_incorrect_information_given = Template(
    "أعتذر على هذا الخطأ. من فضلكم أخبروني بنوع المعلومة التي تلقّيتموها حتى "
    "أتمكن من تصحيحها لكم فورًا."
)

complaint_open_unavailable_exam_not_communicated = Template(
    "أعتذر على هذا الإزعاج. من فضلكم أعطوني بياناتكم حتى أراجع الموقف وأتابعه مع الفرع."
)

#### Complaint Handling — Closing Sentences (Bucket B half) ####
# Real, verbatim closing line per complaint type — injected only if the
# resolution-steps workflow (Bucket C) reaches a successful end without
# the escalation guardrail firing. Some contain a real bracketed
# placeholder from the source data itself (e.g. "[يُضاف هنا الرد المناسب]")
# — preserved exactly as written in the sheet, not converted to a
# $variable, since the source itself marks it as manual free text.

complaint_close_branch_or_doctor_misconduct = Template(
    "شكرًا لتواصلكم معنا وإبلاغنا بهذا الأمر. تم تسجيل شكواكم وسيتم التواصل "
    "معكم في أقرب وقت. نعتذر مجددًا عن هذه التجربة."
)

complaint_close_customer_service_misconduct = Template(
    "تم تسجيل شكواكم وسيتم مراجعتها والتواصل معكم. نعتذر عن هذه التجربة "
    "ونؤكد لكم أن هذا لا يمثل معيار خدمتنا."
)

complaint_close_results_delay = Template(
    "تم إرسال طلب استعجال التقرير الخاص بكم. سيصلكم على الرقم المسجّل في "
    "أقرب وقت ممكن. إذا لم يصل خلال [X ساعة]، تواصلوا معنا مجددًا"
)

complaint_close_contract_issue = Template(
    "1- \tيتم التأكد من التعاقد الخاص بالعميل اي هو نوع التعاقد بالتحديد \n"
    "2- \tيتم الرد علي استفسار العميل إذا كان (مده التحويل – اذا كان في "
    "امكانيه لاحضار التحويل او الموافقه من خلال الفرع ام لا )\n"
    "3- \tاذا كان العميل يشتكي من السعر يجب مراجعه نوع الفحص جيدا و ادخاله "
    "علي السيستم و و ابلاغ العميل به و الاستفسار منه عن السعر الذي تم ابلاغه به "
)

complaint_close_lucky_card_issue = Template(
    "تم مراجعة بياناتكم. [يُضاف هنا الرد المناسب بناءً على نتيجة المراجعة]. "
    "إذا كنتم بحاجة لأي مساعدة إضافية، نحن في الخدمة"
)

complaint_close_appointment_mismatch = Template(
    "تم مراجعة بيانات حجزكم. [يُوضَّح للمريض الموعد الصحيح أو يُعرض عليه إعادة "
    "الجدولة]. نعتذر عن هذا الإرباك."
)

complaint_close_wrong_booking = Template(
    "تم تصحيح حجزكم. [يُؤكَّد الموعد والفحص الصحيح]. نعتذر عن هذا الخطأ "
    "ونشكركم على صبركم."
)

complaint_close_report_error = Template(
    "تم تسجيل شكواكم بالكامل وسيتم التواصل معكم خلال 48 ساعة لمتابعة تصحيح "
    "التقرير. شكرًا لتواصلكم."
)

complaint_close_exam_delay_at_branch = Template(
    "تم تسجيل ملاحظتكم وسيتم متابعتها مع الفرع. نعتذر عن الانتظار ونشكركم "
    "على صبركم."
)

complaint_close_wrong_results_received = Template(
    "تم تسجيل الشكوى وسيتم التواصل معكم لتصحيح الأمر في أقرب وقت. نعتذر عن "
    "هذا الخطأ."
)

complaint_close_pricing_error = Template(
    "تم مراجعة الأمر. [يُوضَّح للمريض السعر الصحيح أو يُبلَّغ بفتح شكوى رسمية]. "
    "شكرًا لتواصلكم."
)

complaint_close_incorrect_information_given = Template(
    "المعلومة الصحيحة هي [يُذكر التصحيح]. نعتذر عن الخطأ السابق ونشكركم على تنبيهنا."
)

complaint_close_unavailable_exam_not_communicated = Template(
    "تم تسجيل شكواكم وسيتم التواصل معكم لمتابعة الأمر. نعتذر بشدة على هذا الإزعاج."
)
