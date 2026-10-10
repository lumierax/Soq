# إصدار iPhone عبر GitHub

GitHub يبني التطبيق على جهاز Mac سحابي. تثبيت IPA على iPhone يحتاج توقيع Apple صالح؛ امتلاك GitHub وحده لا يكفي. التطبيق يتطلب iOS 15 أو أحدث.

**إذا أردت رفع ZIP دون فكّه من الهاتف:** اتبع [مسار الملف المضغوط وYML المنفصل](PHONE_UPLOAD_AR.md). يتولى workflow استيراد المصادر ثم بناء التطبيق. الخطوات التالية تخص رفع محتويات المصدر بعد فك الضغط.

## تجربة البناء دون شهادة

1. فك ضغط الحزمة، وارفع **محتويات مجلد `NABD` إلى جذر المستودع** الخاص، بحيث تكون `.github` و`frontend` و`backend` مباشرة في الجذر. رفع ZIP وحده يحتاج workflow الاستيراد المنفصل المذكور أعلاه؛ وضع المصدر داخل مجلد إضافي لن يشغّل workflow البناء المعتاد. احتفظ بـ `frontend/package-lock.json` ومجلد `frontend/ios`.
2. من **Actions → Build iOS IPA → Run workflow** اترك `sign_ipa` غير مفعّل.
3. حمّل **Nabd-iOS-unsigned-needs-signing** بعد نجاح البناء.

النتيجة تحتوي IPA غير موقع وأرشيف جهاز iPhone حقيقي. الملف غير الموقع لا يُثبت مباشرة؛ وقّعه بوسيلة موثوقة تملك فيها حساب Apple وشهادة وملف provisioning مناسبين. توقيع الحساب المجاني له قيود ومدة محدودة، ودعم إعادة توقيع التطبيقات يعتمد على أداة التثبيت.

## إصدار IPA موقع

أنشئ App ID باسم `dev.nabd.app` في Apple Developer، وشهادة بصيغة `.p12` تشمل المفتاح الخاص، وملف provisioning مناسب. استخدم **Ad Hoc** للأجهزة المسجلة، أو **Development** للتطوير، أو **App Store** لـ App Store Connect وTestFlight. أيقونة الاختيار في Actions يجب أن تطابق الملف.

أضف القيم التالية في **Settings → Secrets and variables → Actions → New repository secret**:

| الاسم | القيمة |
|---|---|
| `IOS_P12_BASE64` | محتوى ملف شهادة `.p12` محولًا إلى base64 |
| `IOS_P12_PASSWORD` | كلمة مرور ملف `.p12`؛ استخدم كلمة مرور غير فارغة |
| `IOS_PROFILE_BASE64` | محتوى `.mobileprovision` محولًا إلى base64 |
| `IOS_TEAM_ID` | معرف فريق Apple، مثل `ABCDE12345` |

لتحويل الملف إلى base64 على Mac:

```bash
base64 -i certificate.p12 | pbcopy
base64 -i app.mobileprovision | pbcopy
```

نفّذ الأمر الأول والصق القيمة في سر الشهادة، ثم نفّذ الثاني والصقها في سر ملف provisioning. لا تضع الشهادات أو مفاتيح Binance في الكود أو المحادثة.

شغّل workflow مع `sign_ipa` مفعّلًا وحدد طريقة التصدير. سيظهر ملف **Nabd-iOS-signed-…** بعد نجاحه. إصدار Ad Hoc أو Development يعمل على الأجهزة المدرجة داخل الملف؛ إصدار App Store يُرفع إلى App Store Connect وتوزّعه من TestFlight أو المتجر. البناء لا ينشر التطبيق تلقائيًا.

للبناء على Mac محليًا بعد تثبيت Node 22 وXcode؛ المشروع يستخدم Swift Package Manager:

```bash
cd frontend
npm ci
cd ..
bash scripts/build-ios.sh --unsigned
# للبناء الموقع: اضبط الأسرار الأربعة كمتغيرات بيئة وثبّت مساعد التوقيع:
gem install xcodeproj -v 1.27.0 --no-document
bash scripts/build-ios.sh --signed --export-method ad-hoc
```

اختر مجلد مخرجات جديدًا عبر `NABD_IOS_OUTPUT_DIR` عند إعادة البناء؛ مثال: `NABD_IOS_OUTPUT_DIR="$PWD/build/ios-next" bash scripts/build-ios.sh --unsigned`.

## تشغيل السوق والبوت

افتح التطبيق بوضع **مباشر من Binance**: الشموع والشارت والتحليل تعمل دون رابط خادم أو `APP_TOKEN`. الاتصال يحتاج أن تسمح Binance بالوصول من جهازك. الإصدار القديم الذي يطلب خادمًا يحتاج إعادة بناء IPA بهذه النسخة ثم توقيعه.

للبوت الحقيقي وإشعارات تليجرام: [شغّله على جهازك](TELEGRAM_AR.md) ثم اختر «خادم البوت» وأدخل عنوان **HTTPS** لذلك الجهاز و`APP_TOKEN`. مفاتيح التداول وتليجرام تحفظ عليه؛ لا توضع داخل IPA أو GitHub. البوت يعمل أثناء إغلاق الهاتف ما دام جهازه يعمل، لأن iOS لا يضمن تشغيل التطبيق في الخلفية طوال الوقت. عنوان Pages أو `localhost` على iPhone لا يصلح كرابط البوت.
