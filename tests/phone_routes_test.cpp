// The phone routes of the real app: Main.qml with the backend and the control
// program, isolated (own TOMACCO_HOME, XDG and QSettings), on an offscreen
// screen as narrow as a phone. No device, no network: Android emulators are
// AVD files in the isolated home that the real android.avds reads, other
// devices and jobs are injected as data, and device tools stay trapped by the
// runner (.scratch/run_qt.sh). Every check drives the interface with real
// mouse, wheel and key events at 360, 393 and 412 px and 100, 175 and 200 %.
#include "backend.h"
#include "control.h"
#include "nativeview.h"
#include <QGuiApplication>
#include <QClipboard>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJSValue>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickItem>
#include <QQuickWindow>
#include <QSettings>
#include <QTemporaryDir>
#include <QWheelEvent>
#include <QElapsedTimer>
#include <QPointer>
#include <QScopeGuard>
#include <QtTest>
#include <QtWebEngineQuick/qtwebenginequickglobal.h>
#include <functional>
#include <memory>

static const QString longAvd=QStringLiteral("Pixel_9_Pro_XL_API_36_Testgeraet_mit_sehr_langem_Namen");
static const QString longNickname=QString::fromUtf8("Pixel 9 Pro XL — Prüfgerät für sehr lange Gerätenamen auf schmalen Telefonen ✓");
static const QString longFile=QString::fromUtf8("lib/ünïcödé_очень_длинное_имя_файла_mit_Überlänge_für_schmale_Telefone_العربية.dart");

class PhoneRoutesTest : public QObject {
    Q_OBJECT
    QTemporaryDir directory;
    std::unique_ptr<Backend> backend;
    std::unique_ptr<Control> control;
    std::unique_ptr<QQmlApplicationEngine> engine;
    QQuickWindow *window=nullptr;
    QObject *workspace=nullptr,*native=nullptr;
    QStringList qmlErrors;
    int scale=100;

    int px(double value) const { return qRound(value*scale/100.0); }
    static QVariantMap mapOf(const QVariant &value) { return value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant().toMap():value.toMap(); }
    static QQuickItem *find(QQuickItem *parent,const QString &name,bool shownOnly) {
        if(parent->objectName()==name && (!shownOnly || parent->isVisible()))return parent;
        for(auto *child:parent->childItems())if(auto *found=find(child,name,shownOnly))return found;
        return nullptr;
    }
    // The item of that name that is on screen (a hidden desktop twin does not count).
    QQuickItem *shown(const QString &name) { return find(window->contentItem(),name,true); }
    QQuickItem *any(const QString &name) { return find(window->contentItem(),name,false); }
    QRectF sceneRect(QQuickItem *item) { return item->mapRectToScene(QRectF(0,0,item->width(),item->height())); }
    // Nothing important sticks out of the window at the sides.
    bool insideWidth(QQuickItem *item) { const auto r=sceneRect(item);return r.left()>=-0.5 && r.right()<=window->width()+0.5; }
    static QQuickItem *flickableAbove(QQuickItem *item) { for(auto *p=item->parentItem();p;p=p->parentItem())if(p->inherits("QQuickFlickable"))return p;return nullptr; }
    // Scroll with the wheel until the item is inside every scrolling area around it.
    void reveal(QQuickItem *target) {
        QPointer<QQuickItem> item(target);
        for(QPointer<QQuickItem> f=flickableAbove(item);item && f;f=item && f?flickableAbove(f):nullptr) {
            for(int i=0;i<80;++i) {
                const auto r=item->mapRectToItem(f,QRectF(0,0,item->width(),item->height()));
                if(!item || !f)return;
                if(r.top()>=-0.5 && (r.bottom()<=f->height()+0.5 || r.top()<=f->height()/3))break;
                const double before=f->property("contentY").toDouble();
                const auto c=f->mapToScene(QPointF(f->width()/2,qMin(f->height()/2,qreal(20))));
                QWheelEvent wheel(c,window->mapToGlobal(c),QPoint(),QPoint(0,r.top()<0?120:-120),Qt::NoButton,Qt::NoModifier,Qt::NoScrollPhase,false);
                QCoreApplication::sendEvent(window,&wheel);QTest::qWait(30);
                if(!item || !f)return;
                // A wheel taken by an inner list: move this area itself.
                if(qFuzzyCompare(before+1,f->property("contentY").toDouble()+1))
                    f->setProperty("contentY",qMax(0.0,before+(r.top()<0?r.top():r.bottom()-f->height())));
            }
        }
    }
    void tap(QQuickItem *target) {
        QVERIFY2(target,"no item to tap");
        QPointer<QQuickItem> item(target);const QString name=target->objectName();
        reveal(item);
        if(!item && !name.isEmpty())item=shown(name);
        QVERIFY2(item,qPrintable(name+" went away"));
        QVERIFY2(item->isVisible() && item->isEnabled(),qPrintable(item->objectName()));
        // A press on a list that still glides would only stop it: wait until every area is still.
        for(QPointer<QQuickItem> f=flickableAbove(item);f;f=f?flickableAbove(f):nullptr){QElapsedTimer t;t.start();while(f->property("moving").toBool() && t.elapsed()<2000)QTest::qWait(20);if(f->property("moving").toBool())QMetaObject::invokeMethod(f,"cancelFlick");}
        QTest::qWait(30);
        if(!item && !name.isEmpty())item=shown(name);
        QVERIFY2(item,qPrintable(name+" went away"));
        QRectF area=sceneRect(item).intersected(QRectF(0,0,window->width(),window->height()));
        if(auto *f=flickableAbove(item))area=area.intersected(sceneRect(f));
        const auto point=(area.isEmpty()?sceneRect(item):area).center().toPoint();
        QVERIFY2(point.x()>=0 && point.y()>=0 && point.x()<window->width() && point.y()<window->height(),qPrintable(QString("%1 is off screen at %2,%3 in %4x%5").arg(item->objectName()).arg(point.x()).arg(point.y()).arg(window->width()).arg(window->height())));
        // PHONE_TAP_DEBUG=1 names what lies under each tap, from the window down.
        if(qEnvironmentVariableIsSet("PHONE_TAP_DEBUG")){QStringList chain;QQuickItem *at=window->contentItem();while(at){chain<<QString(at->metaObject()->className())+"/"+at->objectName();const auto local=at->mapFromScene(point);QQuickItem *next=at->childAt(local.x(),local.y());if(!next)break;at=next;}qWarning("tap %s at %d,%d hits %s",qPrintable(name),point.x(),point.y(),qPrintable(chain.join(" > ")));}
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,point);
        QTest::qWait(60);
    }
    void tap(const QString &name) { auto *item=shown(name);QVERIFY2(item,qPrintable(name+" is not shown"));tap(item); }
    // A phone target: 48 logical px at least, scaled with the interface, high and (for icons) wide.
    void touchTarget(QQuickItem *item,bool icon=false) {
        QVERIFY2(item,"no target");
        // A row shown a moment ago gets its size at the next layout pass.
        QTRY_VERIFY_WITH_TIMEOUT(item->height()>=px(48)-0.5,1000);
        QVERIFY2(item->height()>=px(48)-0.5,qPrintable(QString("%1 is %2 high at %3 %").arg(item->objectName()).arg(item->height()).arg(scale)));
        if(icon)QVERIFY2(item->width()>=px(48)-0.5,qPrintable(QString("%1 is %2 wide").arg(item->objectName()).arg(item->width())));
    }
    void setSize(int width,int height,int percent) {
        scale=percent;backend->setUiScale(percent);
        window->resize(width,height);
        QTRY_COMPARE(window->width(),width);QTRY_COMPARE(window->height(),height);
        QTest::qWait(80);
    }
    void showTab(const QString &tab) { QVERIFY(QMetaObject::invokeMethod(workspace,"showTab",Q_ARG(QVariant,tab)));QTest::qWait(50); }
    void injectDevices() {
        workspace->setProperty("devices",QVariantList{
            QVariantMap{{"id","emulator-5554"},{"name","Pixel 9 Pro XL"},{"avd",longAvd},{"platform","android"},{"state","device"}},
            QVariantMap{{"id","R5CT1234567890ABCDEFGHIJ"},{"name","Galaxy S24 Ultra mit einem langen Namen"},{"platform","android"},{"state","unauthorized"}}});
    }
    // The library lists AVDs again after a while and on showing; its answer comes from the files above.
    void waitForAvds() {
        auto *library=any("androidDeviceLibrary");QVERIFY(library);
        QTRY_VERIFY_WITH_TIMEOUT(library->property("avds").toList().size()==2,8000);
    }
    void writeFile(const QString &relative,const QByteArray &data) {
        const auto path=directory.filePath(relative);QVERIFY(QDir().mkpath(QFileInfo(path).absolutePath()));
        QFile file(path);QVERIFY(file.open(QIODevice::WriteOnly));file.write(data);
    }
    // Real key presses, one per character, to whatever has the focus.
    void typeKeys(const char *text) { for(const char *c=text;*c;++c)QTest::keyClick(window,*c); QTest::qWait(30); }
    // A list builds rows near its viewport only: wheel through it until the row exists.
    QQuickItem *rowInList(const QString &list,const QString &row) {
        auto *view=shown(list);if(!view)return nullptr;
        view->setProperty("contentY",0);QTest::qWait(30);
        for(int i=0;i<60 && !shown(row);++i){
            const auto c=view->mapToScene(QPointF(view->width()/2,view->height()/2));
            QWheelEvent wheel(c,window->mapToGlobal(c),QPoint(),QPoint(0,-120),Qt::NoButton,Qt::NoModifier,Qt::NoScrollPhase,false);
            QCoreApplication::sendEvent(window,&wheel);QTest::qWait(40);
        }
        return shown(row);
    }
    // TOMACCO_UI_CAPTURES=DIR saves what the window shows, for the design file and the docs.
    void capture(const QString &name) {
        const auto out=qEnvironmentVariable("TOMACCO_UI_CAPTURES");if(out.isEmpty())return;
        QDir().mkpath(out);QTest::qWait(120);
        window->grabWindow().save(QString("%1/phone-%2-%3-%4.png").arg(out,name).arg(window->width()).arg(scale));
    }
    void noErrors() { QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n'))); }
private slots:
    void initTestCase() {
        QVERIFY(directory.isValid());
        qputenv("TOMACCO_HOME",directory.filePath("state").toUtf8());
        qputenv("TOMACCO_UPDATES","off");
        qputenv("XDG_CONFIG_HOME",directory.filePath("config").toUtf8());
        qputenv("XDG_CACHE_HOME",directory.filePath("cache").toUtf8());
        qputenv("XDG_STATE_HOME",directory.filePath("state-home").toUtf8());
        // Two emulators as an Android SDK leaves them, in the AVD folder Tomacco manages.
        writeFile("state/avds/"+longAvd+".ini","avd.ini.encoding=UTF-8\npath="+directory.filePath("state/avds/"+longAvd+".avd").toUtf8()+"\n");
        writeFile("state/avds/"+longAvd+".avd/config.ini","hw.lcd.width=1344\nhw.lcd.height=2992\nhw.lcd.density=480\nhw.gpu.mode=host\nhw.ramSize=4096M\nhw.cpu.ncore=4\n");
        writeFile("state/avds/Small_Phone.ini","path="+directory.filePath("state/avds/Small_Phone.avd").toUtf8()+"\n");
        writeFile("state/avds/Small_Phone.avd/config.ini","hw.lcd.width=720\nhw.lcd.height=1280\n");
        writeFile("state/device-labels.json",QJsonDocument(QJsonObject{{"avd:"+longAvd,QJsonObject{{"nickname",longNickname},{"pinned",true}}}}).toJson());
        // A stand-in adb, first on PATH: it only prints a prerecorded list (one running emulator,
        // one phone that has not allowed USB debugging) and touches no device or network.
        writeFile("bin/adb",QByteArray("#!/bin/sh\ncase \"$*\" in\n")
            +"  'devices -l') printf 'List of devices attached\\nemulator-5554\\tdevice product:sdk model:Pixel_9_Pro_XL transport_id:1\\nR5CT1234567890ABCDEFGHIJ\\tunauthorized transport_id:2\\n';;\n"
            +"  *'emu avd name'*) printf '"+longAvd.toUtf8()+"\\nOK\\n';;\n"
            +"  *'sys.boot_completed'*) echo 1;;\n"
            +"  *) echo \"stand-in adb: $*\" >&2; exit 1;;\nesac\n");
        QVERIFY(QFile::setPermissions(directory.filePath("bin/adb"),QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner));
        qputenv("PATH",directory.filePath("bin").toUtf8()+":"+qgetenv("PATH"));
        // A Flutter project with a long Unicode file name and a match to search for.
        writeFile("project/pubspec.yaml","name: tomacco_phone_test\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n");
        writeFile("project/lib/main.dart","void main() {\n  // needle in main\n  runApp(const App());\n}\n");
        writeFile("project/"+longFile,QByteArray("// ")+QByteArray(400,'x')+"\nconst needle = 'a very long line that has to scroll sideways on a phone rather than wrap';\n");
        QSettings().setValue("app/lastVersion",QCoreApplication::applicationVersion());
        backend=std::make_unique<Backend>(QString());
        Backend::applyInterfaceFont();
        qunsetenv("TOMACCO_LANGUAGE");backend->setLanguage("en");
        backend->setUiScale(100);
        control=std::make_unique<Control>("phone-routes");
        engine=std::make_unique<QQmlApplicationEngine>();
        connect(engine.get(),&QQmlEngine::warnings,this,[this](const QList<QQmlError> &errors){for(const auto &e:errors)if(e.url().toString().startsWith("qrc:/qml/"))qmlErrors.append(e.toString());});
        disconnect(engine.get(),&QQmlEngine::quit,nullptr,nullptr);
        // As main.cpp does: a language chosen while running retranslates the interface at once.
        connect(backend.get(),&Backend::languageChanged,engine.get(),[this]{engine->setUiLanguage(backend->uiLanguage());engine->retranslate();});
        auto *context=engine->rootContext();
        context->setContextProperty("Studio",backend.get());context->setContextProperty("Agent",control.get());
        context->setContextProperty("initialProject",directory.filePath("project"));
        context->setContextProperty("initialWorkspace","runtime");
        context->setContextProperty("initialDevice","");
        for(const auto &key:{"smokeTest","captureMode","initialLandscape","initialUnfolded","initialDeviceOnly"})context->setContextProperty(key,false);
        engine->load(QUrl("qrc:/qml/Main.qml"));
        QVERIFY2(!engine->rootObjects().isEmpty(),qPrintable(qmlErrors.join('\n')));
        window=qobject_cast<QQuickWindow *>(engine->rootObjects().first());QVERIFY(window);
        QVERIFY(QTest::qWaitForWindowExposed(window));window->requestActivate();
        // The offscreen screen is a phone's (main() below), so the window may be as narrow as it.
        QVERIFY2(window->minimumWidth()<=360,qPrintable(QString::number(window->minimumWidth())));
        workspace=window->findChild<QObject *>("workspacePanel");native=window->findChild<QObject *>("nativeWorkspace");
        QVERIFY(workspace);QVERIFY(native);
        QTRY_VERIFY_WITH_TIMEOUT(control->operations().size()>100,10000);
        QTRY_COMPARE_WITH_TIMEOUT(mapOf(workspace->property("project")).value("path").toString(),directory.filePath("project"),10000);
        noErrors();
    }
    void cleanup() { QTest::keyClick(window,Qt::Key_Escape);backend->setUiScale(100);QTest::qWait(30); }

    void phone_shell_has_bottom_navigation_and_more_data() {
        QTest::addColumn<int>("width");QTest::addColumn<int>("percent");
        QTest::newRow("360-100")<<360<<100;QTest::newRow("393-175")<<393<<175;QTest::newRow("412-200")<<412<<200;
    }
    void phone_shell_has_bottom_navigation_and_more() {
        QFETCH(int,width);QFETCH(int,percent);
        setSize(width,852,percent);showTab("project");
        QTRY_VERIFY(native->property("phone").toBool());
        auto *nav=shown("mobileNav");QVERIFY(nav);QVERIFY(insideWidth(nav));
        QVERIFY(sceneRect(nav).bottom()<=window->height()+0.5);
        // Every title bar button stays inside the window: the desktop's extra ones went into More.
        QVERIFY(!shown("settingsButton"));QVERIFY(!shown("aboutButton"));
        QVERIFY(shown("projectButton"));QVERIFY(insideWidth(shown("projectButton")));QVERIFY(insideWidth(shown("commandButton")));
        for(const auto &id:{"project","devices","jobs","files","more"}){auto *entry=shown(QString("mobileNav-")+id);QVERIFY(entry);touchTarget(entry);QVERIFY(insideWidth(entry));}
        // Each page ends above the bottom navigation, at every size: nothing final hides beneath it.
        for(const auto &[tab,page]:{std::pair{"project","projectOverview"},{"devices","devicesPage"},{"files","fileWorkspace"},{"jobs","workspacePanel"}}){
            showTab(tab);QTRY_VERIFY(shown(page));QTest::qWait(50);
            QVERIFY2(sceneRect(shown(page)).bottom()<=sceneRect(shown("mobileNav")).top()+0.5,qPrintable(QString("%1 ends at %2, the navigation starts at %3").arg(page).arg(sceneRect(shown(page)).bottom()).arg(sceneRect(shown("mobileNav")).top())));
        }
        showTab("project");
        tap("mobileNav-more");
        QTRY_VERIFY(shown("mobileNavMore-settings"));
        QVERIFY(shown("mobileNavMore-about"));QVERIFY(shown("mobileNavMore-web-preview"));QVERIFY(shown("mobileNavMore-tools"));
        tap("mobileNavMore-settings");
        auto *settings=window->findChild<QObject *>("settingsDialog");QVERIFY(settings);
        QTRY_VERIFY(settings->property("visible").toBool());
        QTest::keyClick(window,Qt::Key_Escape);QTest::keyClick(window,Qt::Key_Escape);
        QTRY_VERIFY(!settings->property("visible").toBool());
        noErrors();
    }

    void devices_list_detail_and_back_data() { phone_shell_has_bottom_navigation_and_more_data(); }
    void devices_list_detail_and_back() {
        QFETCH(int,width);QFETCH(int,percent);
        setSize(width,852,percent);showTab("devices");
        QVERIFY(QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("android"))));
        workspace->setProperty("phoneDevice",QString());
        waitForAvds();injectDevices();
        QTRY_VERIFY(shown("androidPhoneList"));
        QVERIFY(!shown("deviceActions"));QVERIFY(!shown("androidDeviceGrid"));
        QVERIFY(shown("devicesPhoneHeader"));touchTarget(shown("phoneFindDevices"));QVERIFY(insideWidth(shown("phoneFindDevices")));
        touchTarget(shown("createAndroidEmulatorPhone"));
        // The pinned emulator, with its nickname in full and its state on the title line.
        auto *row=shown("avdRow-"+longAvd);QVERIFY(row);
        QVERIFY(row->height()>=px(56)-0.5);QVERIFY(insideWidth(row));
        auto *title=find(row,"rowTitle",true);QVERIFY(title);
        QCOMPARE(title->property("text").toString(),longNickname);QVERIFY(!title->property("truncated").toBool());QVERIFY(insideWidth(title));
        auto *meta=find(row,"rowMeta",true);if(!meta)meta=find(row,"rowMetaStacked",true);QVERIFY(meta);
        QCOMPARE(meta->property("text").toString(),QString("Connected"));QVERIFY(insideWidth(meta));
        // Even at 200 % in the narrowest window the whole row, wrapped, fits on the screen.
        QVERIFY2(row->height()<window->height()*.8,qPrintable(QString::number(row->height())));
        capture("devices-list");
        injectDevices();QTest::qWait(50);tap("avdRow-"+longAvd);
        QTRY_VERIFY(shown("devicePhoneDetail"));
        QVERIFY(!shown("androidPhoneList"));QVERIFY(!shown("devicesPhoneHeader"));
        QCOMPARE(workspace->property("phoneDevice").toString(),"avd:"+longAvd);
        QCOMPARE(workspace->property("selectedDevice").toString(),QString("emulator-5554"));
        auto *barTitle=shown("devicePhoneBarTitle");QVERIFY(barTitle);QCOMPARE(barTitle->property("text").toString(),longNickname);
        QVERIFY(!barTitle->property("truncated").toBool());QVERIFY(insideWidth(barTitle));
        capture("devices-detail");
        auto *back=shown("devicePhoneBarBack");touchTarget(back,true);QVERIFY(insideWidth(back));
        auto *primary=shown("devicePhonePrimary");QVERIFY(primary);touchTarget(primary);QVERIFY(insideWidth(primary));
        QCOMPARE(primary->property("text").toString(),QString("Open live device"));QVERIFY(primary->isEnabled());
        QVERIFY(shown("devicePhoneActions"));QVERIFY(insideWidth(shown("devicePhoneActions")));
        for(const auto &name:{"devicePhonePin","devicePhoneRename","devicePhoneManageAvd","devicePhonePerformance"}){auto *r=shown(name);QVERIFY2(r,name);QVERIFY(insideWidth(r));QVERIFY(r->height()>=px(48)-0.5);}
        QCOMPARE(find(shown("devicePhonePin"),"rowTitle",true)->property("text").toString(),QString("Unpin"));
        // Manage opens its own dialog with its own confirmation; Escape closes it and the page stays.
        tap("devicePhoneManageAvd");
        auto *manage=window->findChild<QObject *>("manageAvdDialog");
        if(manage){QTRY_VERIFY(manage->property("visible").toBool());auto *popup=qobject_cast<QQuickItem *>(manage->property("contentItem").value<QObject *>());if(popup)QVERIFY(sceneRect(popup).right()<=window->width()+0.5);QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!manage->property("visible").toBool());}
        QVERIFY(shown("devicePhoneDetail"));
        // Escape and the system back key return to the list.
        QTest::keyClick(window,Qt::Key_Escape);
        QTRY_VERIFY(shown("androidPhoneList"));QCOMPARE(workspace->property("phoneDevice").toString(),QString());
        injectDevices();tap(shown("phoneRow-R5CT1234567890ABCDEFGHIJ"));
        QTRY_VERIFY(shown("devicePhoneDetail"));
        auto *state=find(shown("devicePhoneState"),"fact",true);QVERIFY(state);QVERIFY(state->property("text").toString().contains("Not authorized"));
        QVERIFY(!shown("devicePhonePrimary")->isEnabled());QVERIFY(!shown("devicePhoneActions"));
        QVERIFY(insideWidth(shown("devicePhoneBarSubtitle")));
        QTest::keyClick(window,Qt::Key_Back);
        QTRY_VERIFY(shown("androidPhoneList"));
        // A device that went away says so and offers to look again; the back target still works.
        workspace->setProperty("phoneDevice",QString("avd:Gone_Device"));
        QTRY_VERIFY(shown("devicePhoneFind"));QVERIFY(!shown("devicePhonePrimary"));
        tap("devicePhoneBarBack");QTRY_VERIFY(shown("androidPhoneList"));
        // The Apple list on a phone: rows that open the same page.
        tap("devicesPhone-apple");
        QTRY_COMPARE(workspace->property("selectedPlatform").toString(),QString("ios"));
        workspace->setProperty("devices",QVariantList{QVariantMap{{"id","00008110-001A2B3C4D5E6F70"},{"name","iPhone 16 Pro"},{"platform","ios"},{"state","device"}}});
        QTRY_VERIFY(shown("phoneRow-00008110-001A2B3C4D5E6F70"));
        tap("phoneRow-00008110-001A2B3C4D5E6F70");
        QTRY_VERIFY(shown("devicePhoneDetail"));QVERIFY(shown("devicePhoneActions"));QVERIFY(!shown("devicePhoneManage"));
        tap("devicePhoneBarBack");
        QVERIFY(QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("android"))));
        noErrors();
    }

    void files_browse_search_and_file_data() { phone_shell_has_bottom_navigation_and_more_data(); }
    void files_browse_search_and_file() {
        QFETCH(int,width);QFETCH(int,percent);
        setSize(width,852,percent);showTab("files");
        auto *files=any("fileWorkspace");QVERIFY(files);
        files->setProperty("phonePane",QString("browse"));files->setProperty("searching",false);
        QTRY_VERIFY(shown("fileBrowserPane"));QVERIFY(!shown("fileEditorPane"));
        QTRY_VERIFY(shown("filePhoneRow-lib"));
        auto *root=shown("filePhoneRoot");QVERIFY(root);QVERIFY(insideWidth(root));QVERIFY(!root->property("truncated").toBool());
        // Open the folder, then the file with the long name: it wraps, nothing is cut.
        if(!mapOf(files->property("expanded")).value("lib").toBool())tap("filePhoneRow-lib");
        QTRY_VERIFY(mapOf(files->property("expanded")).value("lib").toBool());
        QVERIFY(rowInList("fileList","filePhoneRow-"+longFile));
        auto *row=shown("filePhoneRow-"+longFile);QVERIFY(row->height()>=px(56)-0.5);QVERIFY(insideWidth(row));
        auto *title=find(row,"rowTitle",true);QVERIFY(!title->property("truncated").toBool());QVERIFY(title->property("lineCount").toInt()>=2);QVERIFY(insideWidth(title));
        tap("filePhoneRow-"+longFile);
        QTRY_VERIFY(shown("fileEditorPane"));QVERIFY(!shown("fileBrowserPane"));
        QCOMPARE(files->property("current").toString(),longFile);
        auto *barTitle=shown("filePhoneBarTitle");QVERIFY(barTitle);QVERIFY(!barTitle->property("truncated").toBool());QVERIFY(insideWidth(barTitle));
        touchTarget(shown("filePhoneBarBack"),true);capture("files-file");
        for(const auto &name:{"filePhoneSave","filePhoneCopyPath","filePhoneReload"}){auto *b=shown(name);QVERIFY2(b,name);touchTarget(b);QVERIFY(insideWidth(b));}
        QVERIFY(!shown("filePhoneSave")->isEnabled());
        // The code keeps its lines and scrolls sideways inside its own area.
        auto *code=shown("fileCodeScroll");QVERIFY(code);QVERIFY(insideWidth(code));
        auto *editor=shown("sourceEditor");QVERIFY(editor);QVERIFY(editor->implicitWidth()>code->width());
        tap("filePhoneCopyPath");
        QCOMPARE(QGuiApplication::clipboard()->text(),directory.filePath("project/"+longFile));
        // Back, and the file stays the current one, marked in the list.
        tap("filePhoneBarBack");
        QTRY_VERIFY(shown("fileBrowserPane"));QCOMPARE(files->property("current").toString(),longFile);
        QVERIFY(find(shown("filePhoneRow-"+longFile),"rowTitle",true));QVERIFY(shown("filePhoneRow-"+longFile)->property("highlighted").toBool());
        // Search: typed, Return, results that open the file at the line.
        auto *search=shown("fileSearchField");QVERIFY(search);search->setProperty("text",QString());tap(search);
        QTRY_VERIFY(search->hasActiveFocus());
        typeKeys("needle");QTest::keyClick(window,Qt::Key_Return);
        QTRY_VERIFY_WITH_TIMEOUT(files->property("searching").toBool() && files->property("matches").toList().size()>=2,8000);
        QVERIFY(shown("filePhoneRoot")->property("text").toString().contains("needle"));
        QVERIFY(rowInList("fileList","filePhoneRow-lib/main.dart:2"));
        capture("files-search");
        tap("filePhoneRow-lib/main.dart:2");
        QTRY_COMPARE(files->property("current").toString(),QString("lib/main.dart"));
        QVERIFY(shown("fileEditorPane"));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(shown("fileBrowserPane"));
        // Nothing found: said so in words.
        search->setProperty("text",QString());tap(search);
        typeKeys("zz_nothing_here_zz");QTest::keyClick(window,Qt::Key_Return);
        QTRY_VERIFY_WITH_TIMEOUT(shown("filesNoMatch"),8000);QVERIFY(insideWidth(shown("filesNoMatch")));
        // Loading and a failed list: their own words, not an empty pane.
        tap("fileReloadList");
        files->setProperty("entries",QVariantList{});files->setProperty("listing",true);
        QTRY_VERIFY(shown("filesLoading"));
        files->setProperty("listing",false);files->setProperty("notice",QString("files.list failed: permission denied"));
        QTRY_VERIFY(shown("filesPhoneError"));QVERIFY(insideWidth(shown("filesPhoneError")));
        files->setProperty("notice",QString());QVERIFY(QMetaObject::invokeMethod(files,"refresh"));
        QTRY_VERIFY(shown("filePhoneRow-lib"));
        noErrors();
    }

    void project_list_overview_and_options_data() { phone_shell_has_bottom_navigation_and_more_data(); }
    void project_list_overview_and_options() {
        QFETCH(int,width);QFETCH(int,percent);
        setSize(width,852,percent);workspace->setProperty("projectList",false);showTab("project");
        QTRY_VERIFY(shown("projectPhone"));
        shown("projectPhone")->setProperty("moreOptions",false);workspace->setProperty("selectedDevice",QString());
        auto *name=shown("projectPhoneName");QVERIFY(name);QCOMPARE(name->property("text").toString(),QString("project"));
        QVERIFY(insideWidth(shown("projectPhonePath")));QVERIFY(!shown("projectPhonePath")->property("truncated").toBool());
        // One primary action; without a device it builds, with one it builds and runs.
        auto *primary=shown("projectPhonePrimary");touchTarget(primary);QVERIFY(insideWidth(primary));
        QTRY_COMPARE(primary->property("text").toString(),QString("Build"));
        workspace->setProperty("devices",QVariantList{QVariantMap{{"id","emulator-5554"},{"name","Pixel 9 Pro XL"},{"platform","android"},{"state","device"}}});
        workspace->setProperty("selectedDevice",QString("emulator-5554"));
        QTRY_COMPARE(primary->property("text").toString(),QString("Build & run"));
        QVERIFY(primary->property("accent").toBool());
        int accents=0;for(auto *b:shown("projectPhone")->findChildren<QQuickItem *>())if(b->isVisible() && b->property("accent").toBool() && b->inherits("QQuickButton"))++accents;
        QCOMPARE(accents,1);
        capture("project-overview");
        // The less common options stay folded until asked for.
        QVERIFY(!shown("projectPhoneOptions"));
        tap("projectPhoneMore");QTRY_VERIFY(shown("projectPhoneOptions"));
        QVERIFY(insideWidth(shown("projectPhoneOptions")));touchTarget(shown("projectPhonePlan"));
        // Show build steps: the result opens as its own page with back to the tools.
        tap("projectPhonePlan");
        QTRY_COMPARE(workspace->property("section").toString(),QString("tools"));
        QTRY_VERIFY(shown("toolFormColumn"));QVERIFY(!shown("toolListColumn"));
        touchTarget(shown("toolPhoneBarBack"),true);
        tap("toolPhoneBarBack");QTRY_VERIFY(shown("toolListColumn"));QVERIFY(!shown("toolFormColumn"));
        // iOS: the build host is a choice the person makes, not something found.
        showTab("project");
        auto *platform=any("buildPlatform");QVERIFY(platform);platform->setProperty("currentIndex",1);
        QTRY_VERIFY(shown("projectPhoneHost"));touchTarget(shown("projectPhoneHosts"));
        platform->setProperty("currentIndex",0);
        // The project list: search, recent, saved, open folder, import from Xcode; back returns to the project.
        tap("projectPhoneSwitch");
        QTRY_VERIFY(shown("projectPhoneListBar"));
        for(const auto &n:{"projectPhoneSearch","projectPhoneOpenFolder","projectPhoneNew","projectPhoneImportXcode","projectPhoneByPath"}){auto *i=shown(n);QVERIFY2(i,n);QVERIFY(insideWidth(i));QVERIFY(i->height()>=px(48)-0.5);}
        QVERIFY(shown("projectPhoneRecent-tomacco_phone_test")||shown("projectPhoneRecent-project"));capture("project-list");
        auto *search=shown("projectPhoneSearch");tap(search);typeKeys("zz-no-such-project");
        QTRY_VERIFY(shown("projectPhoneNoMatch"));
        search->setProperty("text",QString());
        QTRY_VERIFY(!shown("projectPhoneNoMatch"));QTest::qWait(100);
        workspace->setProperty("selectedOperation",QVariant());
        tap("projectPhoneImportXcode");
        QTRY_COMPARE(mapOf(workspace->property("selectedOperation")).value("name").toString(),QString("project.import_xcode"));
        QTRY_VERIFY(shown("toolFormColumn"));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(shown("toolListColumn"));
        showTab("project");QTRY_VERIFY(shown("projectPhoneListBar"));
        tap("projectPhoneListBarBack");QTRY_VERIFY(shown("projectPhoneName"));
        QVERIFY(!workspace->property("projectList").toBool());
        workspace->setProperty("selectedDevice",QString());
        noErrors();
    }

    void file_choosers_are_phone_sheets_with_touch_targets_data() {
        QTest::addColumn<int>("width");QTest::addColumn<int>("height");QTest::addColumn<int>("percent");QTest::addColumn<int>("keyboard");
        QTest::newRow("320x640-100")<<320<<640<<100<<0;QTest::newRow("360x800-150")<<360<<800<<150<<0;
        QTest::newRow("393x852-175")<<393<<852<<175<<0;QTest::newRow("412x915-200")<<412<<915<<200<<0;
        QTest::newRow("393x852-125-keyboard")<<393<<852<<125<<330;QTest::newRow("360x480-150-short")<<360<<480<<150<<0;
        QTest::newRow("852x393-100-landscape")<<852<<393<<100<<0;QTest::newRow("915x412-150-landscape")<<915<<412<<150<<0;
    }
    void file_choosers_are_phone_sheets_with_touch_targets() {
        // The phone device page's Install and Screenshot open the app's chooser as a sheet over the
        // phone (Qt's FileDialog fallback was a fixed 600x497 window, wider than the phone). Cancel
        // installs nothing and saves nothing.
        QFETCH(int,width);QFETCH(int,height);QFETCH(int,percent);QFETCH(int,keyboard);
        auto *install=window->findChild<QObject *>("installFileDialog");QVERIFY(install);
        auto *screenshot=window->findChild<QObject *>("screenshotFileDialog");QVERIFY(screenshot);
        const auto restore=qScopeGuard([&]{
            for(auto *d:{install,screenshot}){QMetaObject::invokeMethod(d,"close");if(keyboard)d->setProperty("insetBottom",0);}
            workspace->setProperty("phoneDevice",QString());
            QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("android")));
        });
        QTemporaryDir root;QVERIFY(root.isValid());
        const QString base=root.filePath(QString::fromUtf8("Apps mit Leerzeichen #1 · フォルダー"));
        const QString longApp=QString::fromUtf8("Eine sehr lange App für die Übersetzungsprüfung auf schmalen Telefonen العربية.ipa");
        QVERIFY(QDir().mkpath(base+"/Unterordner"));
        for(const auto &name:{longApp,QStringLiteral("app #2 100%.apk"),QStringLiteral("notes.txt")}){QFile f(base+"/"+name);QVERIFY(f.open(QIODevice::WriteOnly));f.write("synthetic");}
        setSize(width,height,percent);
        // The phone layout below 600 logical px of width; the chooser is a sheet also in a short window.
        const bool phone=width<px(600),sheet=phone || height<px(480);
        QSignalSpy installed(install,SIGNAL(accepted()));QSignalSpy saved(screenshot,SIGNAL(accepted()));
        const auto button=[&](const QString &text)->QQuickItem *{
            std::function<QQuickItem *(QQuickItem *)> walk=[&](QQuickItem *i)->QQuickItem *{
                if(!i->isVisible())return nullptr;
                if(i->inherits("QQuickAbstractButton") && i->property("text").toString()==text && i->width()>0)return i;
                for(auto *c:i->childItems())if(auto *f=walk(c))return f;return nullptr;};
            return walk(window->contentItem());
        };
        const auto openFromDevicePage=[&](QObject *dialog,const QString &label) {
            if(!phone){QVERIFY(QMetaObject::invokeMethod(dialog,"open"));return;}
            auto *b=button(label);QVERIFY2(b,qPrintable(label));tap(b);
        };
        if(phone) {
            showTab("devices");QVERIFY(QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("ios"))));
            // The platform's own device list answers first (it would replace the injected phone);
            // a later background list may still do so, so the phone is put back before each click.
            const auto listing=[&]{for(const auto &r:mapOf(workspace->property("pending")))if(r.toMap().value("operation").toString()=="device.list")return true;return false;};
            QTRY_VERIFY_WITH_TIMEOUT(!listing(),10000);
            for(int attempt=0;attempt<8 && !shown("devicePhoneActions");++attempt) {
                workspace->setProperty("devices",QVariantList{QVariantMap{{"id","00008110-001A2B3C4D5E6F70"},{"name","iPhone 16 Pro"},{"platform","ios"},{"state","device"}}});
                QTest::qWait(60);
                if(auto *row=shown("phoneRow-00008110-001A2B3C4D5E6F70")){reveal(row);QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,sceneRect(row).center().toPoint());}
                QTest::qWait(150);
            }
            QTRY_VERIFY(shown("devicePhoneActions"));
        }
        // Open: Install IPA.
        openFromDevicePage(install,"Install IPA");
        QTRY_VERIFY(install->property("opened").toBool());
        const auto check=[&](QObject *dialog,const QString &context) {
            const QString prefix=dialog->objectName();
            QCOMPARE(dialog->property("sheet").toBool(),sheet);
            const QRectF box(dialog->property("x").toReal(),dialog->property("y").toReal(),dialog->property("width").toReal(),dialog->property("height").toReal());
            QVERIFY2(box.left()>=-0.5 && box.top()>=-0.5 && box.right()<=window->width()+0.5 && box.bottom()<=window->height()-keyboard+0.5,
                qPrintable(QString("%1: %2,%3 %4x%5").arg(context).arg(box.x()).arg(box.y()).arg(box.width()).arg(box.height())));
            if(sheet)QVERIFY2(qAbs(box.width()-window->width())<1 && qAbs(box.height()-(window->height()-keyboard))<1,qPrintable(context+": a sheet fills the room above the keyboard"));
            auto *location=shown(prefix+"Location");QVERIFY(location);
            QCOMPARE(location->property("font").value<QFont>().pixelSize(),px(sheet?16:13));
            QVERIFY(location->property("contentWidth").toReal()<=location->width()+1);
            QStringList parts{"Parent","Home","Hidden","-button-0","-button-1","FileName","Filter"};
            for(const auto &part:parts) {
                auto *item=any(prefix+part);QVERIFY2(item,qPrintable(prefix+part));
                reveal(item);QTRY_VERIFY2(item->isVisible(),qPrintable(prefix+part));
                QVERIFY2(insideWidth(item),qPrintable(context+": "+part+" sticks out"));
                const auto r=sceneRect(item);
                QVERIFY2(r.bottom()<=box.bottom()+0.5,qPrintable(context+": "+part+" below the sheet"));
                if(sheet)touchTarget(item,part=="Parent" || part=="Home" ? item->property("displayText").toString().isEmpty() : false);
            }
            // The foot's buttons stay visible above the keyboard without scrolling, each at its natural
            // width (a label is never squeezed letter by letter or cut).
            for(const auto &part:{"-button-0","-button-1"}) {
                auto *b=shown(prefix+part);QVERIFY(sceneRect(b).bottom()<=window->height()-keyboard+0.5);
                const qreal room=b->parentItem()->width();
                QVERIFY2(b->width()>=qMin(b->property("naturalWidth").toReal(),room)-1,qPrintable(QString("%1: %2 is %3 of %4 wide").arg(context,part).arg(b->width()).arg(b->property("naturalWidth").toReal())));
                auto *label=find(b,"studioButtonLabel",true);QVERIFY(label);
                QVERIFY2(!label->property("truncated").toBool(),qPrintable(context+": "+part+" label is cut"));
                QVERIFY2(label->property("lineCount").toInt()==1,qPrintable(context+": "+part+" label breaks"));
            }
            auto *list=shown(prefix+"Folders");QVERIFY(list);QVERIFY(list->height()>=2*dialog->property("rowHeight").toReal()-1);
            const auto filter=shown(prefix+"Filter");QVERIFY(filter);
            auto *label=filter->property("contentItem").value<QQuickItem *>();QVERIFY(!label->property("truncated").toBool());
            QVERIFY(label->property("contentWidth").toReal()<=label->width()+1);
        };
        auto *location=shown("installFileDialogLocation");QVERIFY(location);
        tap(location);QTRY_VERIFY(location->hasActiveFocus());
        QTest::keyClick(window,Qt::Key_A,Qt::ControlModifier);
        {QInputMethodEvent event;event.setCommitString(base);QCoreApplication::sendEvent(QGuiApplication::focusObject(),&event);}
        QTest::keyClick(window,Qt::Key_Return);
        QTRY_COMPARE(mapOf(install->property("listing")).value("path").toString(),base);
        if(keyboard)install->setProperty("insetBottom",keyboard);
        QTest::qWait(60);
        check(install,QString("install %1x%2 %3").arg(width).arg(height).arg(percent));
        // Only apps are listed; every name whole; a tap names the file and chooses nothing.
        QStringList files;for(const auto &e:mapOf(install->property("listing")).value("files").toList())files<<e.toMap().value("name").toString();
        QCOMPARE(files,(QStringList{"app #2 100%.apk",longApp}));
        auto *list=shown("installFileDialogFolders");QVERIFY(list);
        reveal(list);
        for(int i=0;i<files.size();++i) {
            QMetaObject::invokeMethod(list,"positionViewAtIndex",Q_ARG(int,1+i),Q_ARG(int,4));QTest::qWait(40);
            auto *row=shown(QString("installFileDialogFile-%1").arg(i));QVERIFY(row);
            if(sheet)touchTarget(row);QVERIFY(insideWidth(row));
            auto *name=find(row,"fileChooserEntryName",true);QVERIFY(name);
            QCOMPARE(name->property("text").toString(),files[i]);QVERIFY(!name->property("truncated").toBool());
            QVERIFY(name->property("contentWidth").toReal()<=name->width()+1);QVERIFY(insideWidth(name));
        }
        tap(shown("installFileDialogFile-1"));
        QTRY_COMPARE(shown("installFileDialogFileName")?shown("installFileDialogFileName")->property("text").toString():any("installFileDialogFileName")->property("text").toString(),longApp);
        QVERIFY(install->property("visible").toBool());QCOMPARE(installed.count(),0);
        capture(QString("file-chooser-install%1").arg(keyboard?"-keyboard":""));
        reveal(shown("installFileDialog-button-1"));tap("installFileDialog-button-1");
        QTRY_VERIFY(!install->property("visible").toBool());
        QCOMPARE(installed.count(),0);
        for(const auto &request:mapOf(workspace->property("pending")))QVERIFY(request.toMap().value("operation").toString()!="device.install");
        // Save: Screenshot, a typed name; the name field shows above the keyboard.
        openFromDevicePage(screenshot,"Screenshot");
        QTRY_VERIFY(screenshot->property("opened").toBool());
        if(keyboard)screenshot->setProperty("insetBottom",keyboard);
        auto *name=any("screenshotFileDialogFileName");QVERIFY(name);
        QTRY_VERIFY(name->hasActiveFocus());
        QMetaObject::invokeMethod(name,"forceActiveFocus");
        {QInputMethodEvent event;event.setCommitString(QString::fromUtf8("Bildschirmfoto vom iPhone mit einem langen Namen #3"));QCoreApplication::sendEvent(QGuiApplication::focusObject(),&event);}
        QTest::qWait(80);
        QTRY_VERIFY(name->isVisible());
        {
            auto *body=any("screenshotFileDialogBody");QVERIFY(body);
            auto *flick=body->property("contentItem").value<QQuickItem *>();
            const auto field=sceneRect(name),view=sceneRect(flick);
            QVERIFY2(field.top()>=view.top()-0.5 && field.bottom()<=view.bottom()+0.5,qPrintable(QString("the focused name %1..%2 is out of the view %3..%4").arg(field.top()).arg(field.bottom()).arg(view.top()).arg(view.bottom())));
        }
        capture(QString("file-chooser-screenshot%1").arg(keyboard?"-keyboard":""));
        check(screenshot,QString("screenshot %1x%2 %3").arg(width).arg(height).arg(percent));
        QVERIFY(screenshot->property("target").toString().endsWith(QString::fromUtf8("Bildschirmfoto vom iPhone mit einem langen Namen #3")));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!screenshot->property("visible").toBool());
        QCOMPARE(saved.count(),0);
        for(const auto &request:mapOf(workspace->property("pending")))QVERIFY(request.toMap().value("operation").toString()!="device.screenshot");
        noErrors();
    }
    void project_chooser_is_a_phone_sheet_with_touch_targets_data() {
        QTest::addColumn<int>("width");QTest::addColumn<int>("percent");
        QTest::newRow("360-100")<<360<<100;QTest::newRow("360-150")<<360<<150;QTest::newRow("393-125")<<393<<125;
        QTest::newRow("393-175")<<393<<175;QTest::newRow("412-150")<<412<<150;QTest::newRow("412-200")<<412<<200;
    }
    void project_chooser_is_a_phone_sheet_with_touch_targets() {
        // The project list's Open folder opens the app's project chooser as a sheet over the
        // phone (Qt's FolderDialog fallback was a fixed 600x400 window, wider than the phone).
        QFETCH(int,width);QFETCH(int,percent);
        const auto original=directory.filePath("project");
        const auto projectPath=[&]{return mapOf(workspace->property("project")).value("path").toString();};
        auto *dialog=window->findChild<QObject *>("projectFolderDialog");QVERIFY(dialog);
        const auto restore=qScopeGuard([&]{
            QMetaObject::invokeMethod(dialog,"close");workspace->setProperty("projectList",false);
            if(projectPath()!=original){QMetaObject::invokeMethod(workspace,"openProject",Q_ARG(QVariant,original));QTRY_COMPARE_WITH_TIMEOUT(projectPath(),original,10000);}
            for(const auto &folder:backend->recentProjects())if(folder!=QFileInfo(original).canonicalFilePath())backend->forgetProject(folder);
        });
        QTemporaryDir root;QVERIFY(root.isValid());
        const QString base=root.filePath(QString::fromUtf8("Projekte mit Leerzeichen · フォルダー")),project=base+"/Flutter App",link=base+QString::fromUtf8("/Verknüpfung zum Projekt");
        const QStringList names{QString::fromUtf8("Ein sehr langer Projektordner für die Übersetzungsprüfung der Anwendung"),QString::fromUtf8("تطبيق الهاتف المحمول التجريبي الطويل جدا"),
            QString::fromUtf8("とても長い日本語のプロジェクトフォルダー名の確認用"),"Flutter App"};
        for(const auto &name:names)QVERIFY(QDir().mkpath(base+"/"+name));
        {QFile pubspec(project+"/pubspec.yaml");QVERIFY(pubspec.open(QIODevice::WriteOnly));pubspec.write("name: phone_chooser_app\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n");}
        QVERIFY(QFile::link(project,link));
        const auto canonicalProject=QFileInfo(project).canonicalFilePath();
        setSize(width,852,percent);workspace->setProperty("projectList",true);showTab("project");
        QTRY_VERIFY(shown("projectPhoneOpenFolder"));
        tap("projectPhoneOpenFolder");
        QTRY_VERIFY(dialog->property("opened").toBool());
        QCOMPARE(dialog->property("title").toString(),QString("Choose project"));
        QVERIFY(dialog->property("sheet").toBool());
        const QRectF box(dialog->property("x").toReal(),dialog->property("y").toReal(),dialog->property("width").toReal(),dialog->property("height").toReal());
        QVERIFY2(qAbs(box.width()-window->width())<1 && box.left()>=-0.5 && box.bottom()<=window->height()+0.5,qPrintable(QString("sheet %1,%2 %3x%4").arg(box.x()).arg(box.y()).arg(box.width()).arg(box.height())));
        // A typed path (the same field takes a pasted one) shows that folder.
        auto *location=shown("projectFolderDialogLocation");QVERIFY(location);
        tap(location);QTRY_VERIFY(location->hasActiveFocus());
        QTest::keyClick(window,Qt::Key_A,Qt::ControlModifier);
        {QInputMethodEvent event;event.setCommitString(base);QCoreApplication::sendEvent(QGuiApplication::focusObject(),&event);}
        QTest::keyClick(window,Qt::Key_Return);
        const auto listed=[&]{return mapOf(dialog->property("listing")).value("path").toString();};
        QTRY_COMPARE(listed(),base);
        QCOMPARE(location->property("text").toString(),base);
        QCOMPARE(location->property("font").value<QFont>().pixelSize(),px(16));
        // Touch targets inside the screen; the icon buttons keep their names.
        for(const auto &name:{"projectFolderDialogParent","projectFolderDialogHome"}){auto *b=shown(name);QVERIFY2(b,name);touchTarget(b,b->property("displayText").toString().isEmpty());QVERIFY(insideWidth(b));QVERIFY(!b->property("text").toString().isEmpty());}
        touchTarget(shown("projectFolderDialogHidden"));QVERIFY(insideWidth(shown("projectFolderDialogHidden")));
        for(const auto &name:{"projectFolderDialog-button-0","projectFolderDialog-button-1"}){auto *b=shown(name);QVERIFY2(b,name);touchTarget(b);QVERIFY(insideWidth(b));QVERIFY(sceneRect(b).bottom()<=window->height()+0.5);}
        auto *list=shown("projectFolderDialogFolders");QVERIFY(list);QVERIFY(list->height()>=2*px(48)-0.5);
        const auto folders=mapOf(dialog->property("listing")).value("folders").toList();
        QCOMPARE(folders.size(),names.size()+1);
        const auto rowOf=[&](const QString &name)->QQuickItem *{
            int index=-1;for(int i=0;i<folders.size();++i)if(folders[i].toMap().value("name").toString()==name)index=i;
            if(index<0)return nullptr;
            QMetaObject::invokeMethod(list,"positionViewAtIndex",Q_ARG(int,index),Q_ARG(int,4));QTest::qWait(40);
            return shown(QString("projectFolderDialogFolder-%1").arg(index));
        };
        for(const auto &entry:folders) {
            const auto name=entry.toMap().value("name").toString();
            auto *row=rowOf(name);QVERIFY2(row,qPrintable(name));touchTarget(row);QVERIFY(insideWidth(row));
            auto *label=find(row,"projectFolderName",true);QVERIFY(label);
            QCOMPARE(label->property("text").toString(),name);
            QVERIFY2(!label->property("truncated").toBool() && label->property("contentWidth").toReal()<=label->width()+1 && insideWidth(label),qPrintable(name));
        }
        list->setProperty("contentY",0);
        capture("project-chooser");
        // Tap a folder, then Cancel: the open project stays.
        tap(rowOf("Flutter App"));QTRY_COMPARE(listed(),project);
        tap("projectFolderDialog-button-1");QTRY_VERIFY(!dialog->property("visible").toBool());
        QTest::qWait(100);QCOMPARE(projectPath(),original);
        // Again from the list; the link shows its target and Open opens the project it leads to.
        tap("projectPhoneOpenFolder");QTRY_VERIFY(dialog->property("opened").toBool());
        QVERIFY(QMetaObject::invokeMethod(dialog,"go",Q_ARG(QVariant,base)));QTRY_COMPARE(listed(),base);
        auto *linkRow=rowOf(QFileInfo(link).fileName());QVERIFY(linkRow);
        QCOMPARE(find(linkRow,"projectFolderLink",true)->property("text").toString(),QString::fromUtf8("→ ")+canonicalProject);
        tap(linkRow);QTRY_COMPARE(listed(),link);
        QTRY_VERIFY(shown("projectFolderDialog-button-0") && shown("projectFolderDialog-button-0")->isEnabled());
        tap("projectFolderDialog-button-0");QTRY_VERIFY(!dialog->property("visible").toBool());
        QTRY_COMPARE_WITH_TIMEOUT(QFileInfo(projectPath()).canonicalFilePath(),canonicalProject,10000);
        noErrors();
    }

    // The phone chooser sheet in a translated interface at the narrowest phone and largest
    // interface size, right to left in Arabic: its own texts are translated, the buttons mirror,
    // the path still reads from the left, and every control and long folder name stays whole,
    // touch-sized and inside the screen. Cancel leaves the open project.
    void project_chooser_phone_sheet_reads_translated_and_mirrored_data() {
        QTest::addColumn<QString>("language");QTest::addColumn<int>("width");QTest::addColumn<int>("percent");
        QTest::newRow("ar-360-200")<<"ar"<<360<<200;QTest::newRow("de-360-200")<<"de"<<360<<200;QTest::newRow("ja-412-175")<<"ja"<<412<<175;
    }
    void project_chooser_phone_sheet_reads_translated_and_mirrored() {
        QFETCH(QString,language);QFETCH(int,width);QFETCH(int,percent);
        const auto original=directory.filePath("project");
        const auto projectPath=[&]{return mapOf(workspace->property("project")).value("path").toString();};
        auto *dialog=window->findChild<QObject *>("projectFolderDialog");QVERIFY(dialog);
        const auto restore=qScopeGuard([&]{
            QMetaObject::invokeMethod(dialog,"close");workspace->setProperty("projectList",false);
            backend->setLanguage("en");QTest::qWait(50);
        });
        backend->setLanguage(language);QTRY_COMPARE(backend->uiLanguage(),language);
        const bool rightToLeft=language=="ar";QCOMPARE(backend->rightToLeft(),rightToLeft);
        QTemporaryDir root;QVERIFY(root.isValid());
        const QString base=root.filePath(QString::fromUtf8("مشاريع الهاتف · Projekte · プロジェクト"));
        const QStringList names{QString::fromUtf8("تطبيق الهاتف المحمول التجريبي الطويل جدا للتحقق من الترجمة"),
            QString::fromUtf8("Ein sehr langer Projektordner für die Übersetzungsprüfung der Anwendung"),
            QString::fromUtf8("とても長い日本語のプロジェクトフォルダー名の確認用ディレクトリ")};
        for(const auto &name:names)QVERIFY(QDir().mkpath(base+"/"+name));
        setSize(width,852,percent);QTRY_VERIFY(native->property("phone").toBool());
        workspace->setProperty("projectList",true);showTab("project");
        QTRY_VERIFY(shown("projectPhoneOpenFolder"));
        tap("projectPhoneOpenFolder");
        QTRY_VERIFY(dialog->property("opened").toBool());
        QVERIFY(dialog->property("sheet").toBool());
        const auto title=dialog->property("title").toString();
        QCOMPARE(title,QCoreApplication::translate("Workspace","Choose project"));
        QVERIFY2(title!=QString("Choose project"),qPrintable(language+" shows the English title"));
        const QRectF box(dialog->property("x").toReal(),dialog->property("y").toReal(),dialog->property("width").toReal(),dialog->property("height").toReal());
        QVERIFY2(qAbs(box.width()-window->width())<1 && box.left()>=-0.5 && box.bottom()<=window->height()+0.5,qPrintable(QString("sheet %1,%2 %3x%4").arg(box.x()).arg(box.y()).arg(box.width()).arg(box.height())));
        const auto listed=[&]{return mapOf(dialog->property("listing")).value("path").toString();};
        QVERIFY(QMetaObject::invokeMethod(dialog,"go",Q_ARG(QVariant,base)));QTRY_COMPARE(listed(),base);
        auto *location=shown("projectFolderDialogLocation");QVERIFY(location);
        QCOMPARE(location->property("text").toString(),base);
        QVERIFY(insideWidth(location));
        // A path reads from the left also in a mirrored interface.
        QCOMPARE(location->property("effectiveHorizontalAlignment").toInt(),int(Qt::AlignLeft));
        QCOMPARE(location->property("font").value<QFont>().pixelSize(),px(16));
        for(const auto &[part,source]:{std::pair{"projectFolderDialogParent","Parent folder"},{"projectFolderDialogHome","Home folder"}}) {
            auto *b=shown(part);QVERIFY2(b,part);
            const auto text=b->property("text").toString();
            QCOMPARE(text,QCoreApplication::translate("ProjectFolderDialog",source));
            QVERIFY2(text!=QString::fromLatin1(source),qPrintable(language+" has no translation of "+source));
            touchTarget(b,b->property("displayText").toString().isEmpty());QVERIFY(insideWidth(b));
        }
        const auto parentX=sceneRect(shown("projectFolderDialogParent")).center().x(),homeX=sceneRect(shown("projectFolderDialogHome")).center().x();
        QVERIFY2(rightToLeft?parentX>homeX:parentX<homeX,qPrintable(QString("%1 parent %2 home %3").arg(language).arg(parentX).arg(homeX)));
        auto *hidden=shown("projectFolderDialogHidden");QVERIFY(hidden);touchTarget(hidden);QVERIFY(insideWidth(hidden));
        QCOMPARE(hidden->property("text").toString(),QCoreApplication::translate("FileWorkspace","Hidden"));
        for(const auto &name:{"projectFolderDialog-button-0","projectFolderDialog-button-1"}){
            auto *b=shown(name);QVERIFY2(b,name);touchTarget(b);QVERIFY(insideWidth(b));
            QVERIFY(sceneRect(b).bottom()<=window->height()+0.5);QVERIFY(!b->property("text").toString().isEmpty());
        }
        auto *list=shown("projectFolderDialogFolders");QVERIFY(list);QVERIFY(list->height()>=2*px(48)-0.5);
        const auto folders=mapOf(dialog->property("listing")).value("folders").toList();
        QCOMPARE(folders.size(),names.size());
        for(int index=0;index<folders.size();++index) {
            const auto name=folders[index].toMap().value("name").toString();
            QMetaObject::invokeMethod(list,"positionViewAtIndex",Q_ARG(int,index),Q_ARG(int,4));QTest::qWait(40);
            auto *row=shown(QString("projectFolderDialogFolder-%1").arg(index));QVERIFY2(row,qPrintable(name));
            touchTarget(row);QVERIFY(insideWidth(row));
            auto *label=find(row,"projectFolderName",true);QVERIFY(label);
            QCOMPARE(label->property("text").toString(),name);
            QVERIFY2(!label->property("truncated").toBool() && label->property("contentWidth").toReal()<=label->width()+1 && insideWidth(label),qPrintable(name));
        }
        list->setProperty("contentY",0);
        capture("project-chooser-"+language);
        tap("projectFolderDialog-button-1");QTRY_VERIFY(!dialog->property("visible").toBool());
        QTest::qWait(100);QCOMPARE(projectPath(),original);
        noErrors();
    }

    void settings_sheet_and_agent_clients_data() {
        QTest::addColumn<int>("width");QTest::addColumn<int>("percent");
        QTest::newRow("393-100")<<393<<100;QTest::newRow("360-175")<<360<<175;QTest::newRow("412-200")<<412<<200;
    }
    void settings_sheet_and_agent_clients() {
        QFETCH(int,width);QFETCH(int,percent);
        setSize(width,852,percent);showTab("project");
        QVERIFY(QMetaObject::invokeMethod(native,"openSettings",Q_ARG(QVariant,QString())));
        auto *dialog=qobject_cast<QQuickItem *>(window->findChild<QObject *>("settingsDialog")->property("contentItem").value<QObject *>());
        auto *settings=window->findChild<QObject *>("settingsDialog");
        QTRY_VERIFY(settings->property("visible").toBool());
        QVERIFY(settings->property("sheet").toBool());
        QCOMPARE(settings->property("width").toReal(),qreal(width));
        QVERIFY(settings->property("height").toReal()<=window->height());
        QVERIFY(dialog);capture("settings-sheet");
        // Every group is reachable by scrolling and readable within the width.
        for(const auto &n:{"settingsAppearance","settingsInterfaceSize","settingsLanguage","settingsNotifications","settingsAgent","settingsSystem","settingsUpdates"}){
            auto *section=shown(n);QVERIFY2(section,n);reveal(section);
            // A sheet opened at a new size lays itself out on the next pass.
            QTRY_VERIFY_WITH_TIMEOUT(insideWidth(section),1500);
            QVERIFY2(insideWidth(section),n);
        }
        for(const auto &n:{"interfaceSmaller","interfaceLarger","interfaceReset"})touchTarget(shown(n),QString(n)!="interfaceReset");
        for(const auto &n:{"settingsSystem-agent","settingsSystem-hosts","settingsSystem-storage","settingsSystem-diagnostics","settingsSystem-policy"}){auto *r=shown(n);QVERIFY2(r,n);QVERIFY(r->height()>=px(56)-0.5);QVERIFY(insideWidth(r));}
        // Search keeps working on the sheet.
        auto *search=shown("settingsSearch");tap(search);typeKeys("bug report");
        QTRY_VERIFY(shown("settingsSystem-diagnostics"));QVERIFY(!shown("settingsLanguage"));
        // A system row opens its form, explicitly: nothing runs until Run.
        tap("settingsSystem-diagnostics");
        QTRY_VERIFY(!settings->property("visible").toBool());
        QTRY_COMPARE(workspace->property("section").toString(),QString("tools"));
        QCOMPARE(mapOf(workspace->property("selectedOperation")).value("name").toString(),QString("diagnostics.bundle"));
        QTRY_VERIFY(shown("toolFormColumn"));touchTarget(shown("toolRun"));QVERIFY(insideWidth(shown("toolRun")));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(shown("toolListColumn"));
        // The agent clients: their own sheet, each client with its own state.
        QVERIFY(QMetaObject::invokeMethod(native,"openSettings",Q_ARG(QVariant,QString("agent"))));
        QTRY_VERIFY(settings->property("visible").toBool());
        QTRY_VERIFY(shown("settingsSystem-agent"));tap("settingsSystem-agent");
        auto *clients=window->findChild<QObject *>("agentClientDialog");QVERIFY(clients);
        QTRY_VERIFY(clients->property("visible").toBool());
        QVERIFY(clients->property("sheet").toBool());QCOMPARE(clients->property("width").toReal(),qreal(width));
        capture("agent-clients");
        auto *done=shown("agentClientDialogClose");QVERIFY(done);touchTarget(done);QVERIFY(insideWidth(done));
        tap(done);QTRY_VERIFY(!clients->property("visible").toBool());
        noErrors();
    }

    void keyboard_inset_keeps_the_focused_field_visible_data() {
        QTest::addColumn<int>("percent");QTest::newRow("100")<<100;QTest::newRow("200")<<200;
    }
    void keyboard_inset_keeps_the_focused_field_visible() {
        QFETCH(int,percent);
        setSize(393,852,percent);workspace->setProperty("projectList",false);showTab("project");
        QTRY_VERIFY(shown("projectPhoneName"));shown("projectPhone")->setProperty("moreOptions",false);
        tap("projectPhoneMore");QTRY_VERIFY(shown("projectPhoneOptions"));
        // A keyboard covers the lower 45 % of the window: the navigation gives way and the page ends above it.
        native->setProperty("keyboardInset",qreal(qRound(852*.45)));
        QTRY_VERIFY(!shown("mobileNav"));QVERIFY(!shown("statusLine"));
        auto *field=shown("projectPhoneWorkspaceName");QVERIFY(field);
        tap(field);QTRY_VERIFY(field->hasActiveFocus());
        QTest::qWait(150);
        const auto r=sceneRect(field);
        QVERIFY2(r.bottom()<=852-qRound(852*.45)+0.5 && r.top()>=0,qPrintable(QString("field at %1..%2").arg(r.top()).arg(r.bottom())));
        typeKeys("Telefon");
        QCOMPARE(field->property("text").toString(),QString("Telefon"));
        // The settings sheet ends above the keyboard as well.
        QVERIFY(QMetaObject::invokeMethod(native,"openSettings",Q_ARG(QVariant,QString())));
        auto *settings=window->findChild<QObject *>("settingsDialog");QTRY_VERIFY(settings->property("visible").toBool());
        QVERIFY(settings->property("y").toReal()+settings->property("height").toReal()<=852-qRound(852*.45)+0.5);
        QTest::keyClick(window,Qt::Key_Escape);QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!settings->property("visible").toBool());
        native->setProperty("keyboardInset",qreal(0));
        QTRY_VERIFY(shown("mobileNav"));
        // A notch and a gesture bar: the content keeps clear of both.
        native->setProperty("insetTop",qreal(px(28)));native->setProperty("insetBottom",qreal(px(24)));
        QTRY_VERIFY(sceneRect(shown("mobileNav")).bottom()<=852-px(24)+0.5);
        QVERIFY(sceneRect(shown("projectButton")).top()>=px(28)-0.5);
        native->setProperty("insetTop",qreal(0));native->setProperty("insetBottom",qreal(0));
        field->setProperty("text",QString());
        noErrors();
    }

    void jobs_detail_targets_and_back_data() { phone_shell_has_bottom_navigation_and_more_data(); }
    void jobs_detail_targets_and_back() {
        QFETCH(int,width);QFETCH(int,percent);
        setSize(width,852,percent);showTab("jobs");
        // As the desktop suite does: hold the 1.8 s job.list poll, whose real answer does not know this job.
        QList<QObject *> polls;for(auto *child:workspace->children())if(child->property("interval").toInt()==1800 && child->property("running").toBool()){polls.append(child);QMetaObject::invokeMethod(child,"stop");}
        const auto restart=qScopeGuard([&]{for(auto *timer:polls)QMetaObject::invokeMethod(timer,"start");});
        const QVariantMap job{{"id","job-phone-1"},{"label","Build & Run · tomacco_phone_test · Pixel 9 Pro XL — ein langer Jobname, der umbrechen muss"},{"state","running"},{"created",QDateTime::currentSecsSinceEpoch()-75},{"plan",QVariantMap{{"exclusive",directory.filePath("project")}}}};
        workspace->setProperty("jobs",QVariantList{job});workspace->setProperty("selectedJob",QString());
        QTRY_VERIFY(shown("jobRow-job-phone-1"));
        tap("jobRow-job-phone-1");
        QTRY_VERIFY(shown("jobPhoneBar"));QVERIFY(!shown("jobListColumn"));
        auto *title=shown("jobPhoneBarTitle");QVERIFY(!title->property("truncated").toBool());QVERIFY(insideWidth(title));
        touchTarget(shown("jobBack"),true);
        auto *cancel=shown("jobCancelPhone");QVERIFY(cancel);touchTarget(cancel);QVERIFY(insideWidth(cancel));
        QVERIFY(insideWidth(shown("jobPhoneActions")));capture("job-detail");
        tap("jobBack");
        // The control program's own list (polled) does not know this injected job: put it back for the list.
        QTRY_COMPARE(workspace->property("selectedJob").toString(),QString());
        workspace->setProperty("jobs",QVariantList{job});
        QTRY_VERIFY(shown("jobListColumn"));
        workspace->setProperty("jobs",QVariantList{job});tap("jobRow-job-phone-1");
        QTRY_VERIFY(shown("jobPhoneBar"));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_COMPARE(workspace->property("selectedJob").toString(),QString());
        workspace->setProperty("jobs",QVariantList{job});QTRY_VERIFY(shown("jobListColumn"));
        workspace->setProperty("jobs",QVariantList{});
        noErrors();
    }

    void threshold_and_orientation_keep_each_route_state() {
        // Portrait phone, a device page open.
        setSize(393,852,100);showTab("devices");
        QVERIFY(QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("android"))));
        waitForAvds();injectDevices();
        workspace->setProperty("phoneDevice","avd:"+longAvd);
        QTRY_VERIFY(shown("devicePhoneDetail"));
        // Landscape on the same phone: wider than the threshold, the desktop layout with its actions.
        setSize(852,393,100);
        QTRY_VERIFY(!native->property("phone").toBool());
        QTRY_VERIFY(shown("deviceActions"));QVERIFY(!shown("devicePhoneDetail"));QVERIFY(!shown("mobileNav"));
        // A tablet and an external display, then back to the phone: the same device page.
        setSize(768,1024,100);QTRY_VERIFY(shown("deviceActions"));
        setSize(1440,900,100);QTRY_VERIFY(shown("deviceActions"));
        setSize(393,852,100);
        QTRY_VERIFY(shown("devicePhoneDetail"));QCOMPARE(workspace->property("phoneDevice").toString(),"avd:"+longAvd);
        // The same at 200 %: the threshold is in logical px, a 1000 px window is a phone.
        setSize(1000,1400,200);QTRY_VERIFY(native->property("phone").toBool());QTRY_VERIFY(shown("devicePhoneDetail"));
        touchTarget(shown("devicePhoneBarBack"),true);
        // A file stays open across the change, and the handoff band stays reachable on a phone.
        setSize(393,852,100);workspace->setProperty("phoneDevice",QString());
        showTab("files");auto *files=any("fileWorkspace");
        QVERIFY(QMetaObject::invokeMethod(files,"openFile",Q_ARG(QVariant,QString("lib/main.dart")),Q_ARG(QVariant,1)));
        QTRY_VERIFY(shown("fileEditorPane"));QVERIFY(!shown("fileBrowserPane"));
        setSize(1380,900,100);QTRY_VERIFY(shown("fileBrowserPane"));QVERIFY(shown("fileEditorPane"));
        setSize(393,852,100);QTRY_VERIFY(shown("fileEditorPane"));QVERIFY(!shown("fileBrowserPane"));
        QCOMPARE(files->property("current").toString(),QString("lib/main.dart"));
        auto *banner=any("handoffBanner");QVERIFY(banner);
        auto *handoffs=banner->property("model").value<QObject *>();QVERIFY(handoffs);
        handoffs->setProperty("waiting",QVariantList{QVariantMap{{"id","handoff-1"},{"kind","login"},{"reason","Sign in on the device, then tap Done"}}});
        QTRY_VERIFY(banner->isVisible());QVERIFY(insideWidth(banner));
        QVERIFY(sceneRect(banner).bottom()<=window->height());
        handoffs->setProperty("waiting",QVariantList{});
        QTest::keyClick(window,Qt::Key_Escape);
        noErrors();
    }
    // Translated action labels on the real screens at the largest size: German, Tamil and Arabic
    // (mirrored) on a 360 px phone, Japanese on 393 px. Every phone text button on the project page
    // (options open), the open file and the interface size settings stays inside the window, is at
    // least a 48 px target, and shows its whole label: wrapped onto more lines, never cut, inside the
    // button. In the mirrored layout an icon sits at the right, before the text, without covering it.
    void translated_action_labels_stay_whole_data() {
        QTest::addColumn<QString>("language");QTest::addColumn<int>("width");QTest::addColumn<int>("percent");
        QTest::newRow("de-360-200")<<"de"<<360<<200;QTest::newRow("ta-360-200")<<"ta"<<360<<200;
        QTest::newRow("ar-360-200")<<"ar"<<360<<200;QTest::newRow("ja-393-200")<<"ja"<<393<<200;
    }
    void translated_action_labels_stay_whole() {
        QFETCH(QString,language);QFETCH(int,width);QFETCH(int,percent);
        const auto english=qScopeGuard([&]{backend->setLanguage("en");QTest::qWait(50);});
        backend->setLanguage(language);QTRY_COMPARE(backend->uiLanguage(),language);
        QCOMPARE(backend->rightToLeft(),language=="ar");
        setSize(width,852,percent);QTRY_VERIFY(native->property("phone").toBool());
        int buttons=0,wrapped=0;
        auto check=[&](QQuickItem *scope){
            QVERIFY(scope);
            for(auto *b:scope->findChildren<QQuickItem *>()){
                // A phone text button: touch, with its text shown (an icon button names itself by tooltip).
                if(!b->isVisible() || !b->inherits("QQuickButton") || !b->property("touch").toBool() || b->property("iconOnly").toBool())continue;
                ++buttons;const QString what=QString("%1 \"%2\" at %3 %").arg(b->objectName(),b->property("text").toString()).arg(percent);
                QQuickItem *label=find(b,"studioButtonLabel",true);
                for(auto *t:b->findChildren<QQuickItem *>())if(!label && t->inherits("QQuickText") && t->property("text")==b->property("displayText"))label=t;
                QVERIFY2(label,qPrintable(what));
                QVERIFY2(insideWidth(b),qPrintable(what+" sticks out of the window"));
                QVERIFY2(b->height()>=px(48)-0.5,qPrintable(what+" is lower than a touch target"));
                QVERIFY2(!label->property("truncated").toBool(),qPrintable(what+" is cut off"));
                QVERIFY2(label->property("contentWidth").toDouble()<=label->width()+0.5,qPrintable(what+" is wider than its button"));
                const auto br=sceneRect(b),lr=sceneRect(label);
                QVERIFY2(lr.top()>=br.top()-0.5 && lr.bottom()<=br.bottom()+0.5,qPrintable(what+" runs out of its button"));
                if(label->property("lineCount").toInt()>1)++wrapped;
                auto *icon=find(b,"studioButtonIcon",true);
                if(icon && backend->rightToLeft())
                    QVERIFY2(sceneRect(icon).left()>=lr.right()-0.5,qPrintable(what+": the icon is not at the right of the text"));
            }
        };
        // The project page with its less common options open, and a device so the primary action runs.
        workspace->setProperty("projectList",false);showTab("project");QTRY_VERIFY(shown("projectPhone"));
        workspace->setProperty("devices",QVariantList{QVariantMap{{"id","emulator-5554"},{"name","Pixel 9 Pro XL"},{"platform","android"},{"state","device"}}});
        workspace->setProperty("selectedDevice",QString("emulator-5554"));
        shown("projectPhone")->setProperty("moreOptions",true);QTRY_VERIFY(shown("projectPhoneOptions"));QTest::qWait(150);
        // The screen really is in that language: the primary action shows its translation, not English.
        const QString run=QCoreApplication::translate("Workspace","Build & run");QVERIFY2(run!="Build & run",qPrintable(run));
        QTRY_COMPARE(shown("projectPhonePrimary")->property("text").toString(),run);
        check(shown("projectPhone"));if(QTest::currentTestFailed())return;
        reveal(shown("projectPhoneMore"));capture("labels-project-"+language);
        shown("projectPhone")->setProperty("moreOptions",false);workspace->setProperty("selectedDevice",QString());
        // The open file and its actions.
        showTab("files");auto *files=any("fileWorkspace");QVERIFY(files);
        QVERIFY(QMetaObject::invokeMethod(files,"openFile",Q_ARG(QVariant,QString("lib/main.dart")),Q_ARG(QVariant,1)));
        QTRY_VERIFY(shown("fileEditorPane"));QTRY_VERIFY(shown("filePhoneActions"));QTest::qWait(100);
        check(shown("filePhoneActions"));if(QTest::currentTestFailed())return;
        capture("labels-file-"+language);
        // The interface size in the settings sheet.
        QVERIFY(QMetaObject::invokeMethod(native,"openSettings",Q_ARG(QVariant,QString())));
        auto *settings=window->findChild<QObject *>("settingsDialog");QVERIFY(settings);
        QTRY_VERIFY(settings->property("visible").toBool());
        // Every group stays within the sheet (one too wide group widens them all), and so does every button in it.
        for(const auto &n:{"settingsAppearance","settingsInterfaceSize","settingsLanguage","settingsNotifications","settingsAgent","settingsSystem","settingsUpdates","settingsLayouts","settingsTransfer"}){
            auto *section=shown(n);QVERIFY2(section,n);reveal(section);
            QTRY_VERIFY2_WITH_TIMEOUT(insideWidth(section),qPrintable(QString("%1 is wider than the window").arg(n)),1500);
        }
        auto *sheet=shown("settingsInterfaceSize")->parentItem();
        for(auto *b:sheet->findChildren<QQuickItem *>())if(b->isVisible() && b->inherits("QQuickButton"))
            QVERIFY2(insideWidth(b),qPrintable(b->objectName()+" sticks out of the settings sheet"));
        check(sheet);if(QTest::currentTestFailed())return;
        reveal(shown("settingsInterfaceSize"));
        for(const auto &n:{"interfaceSmaller","interfaceLarger"})touchTarget(shown(n),true);
        capture("labels-settings-"+language);
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!settings->property("visible").toBool());
        QVERIFY2(buttons>=12,qPrintable(QString::number(buttons)));
        // Tamil at 360 px and 200 % has labels longer than a row: they must wrap, not be cut.
        if(language=="ta")QVERIFY2(wrapped>0,"no label wrapped");
        qInfo("%s %d x %d %%: %d phone text buttons checked, %d wrapped (\"%s\")",qPrintable(language),width,percent,buttons,wrapped,qPrintable(run));
        noErrors();
    }
    void cleanupTestCase() { engine.reset();control.reset();backend.reset(); }
};

int main(int argc,char **argv) {
    // An offscreen screen the size of a phone at 100 %, so the window may be as narrow as
    // on the Pixel; larger windows still fit (landscape, tablet, external display).
    QTemporaryDir screens;
    QFile config(screens.filePath("screens.json"));
    if(config.open(QIODevice::WriteOnly)){config.write(R"({"screens":[{"name":"phone","x":0,"y":0,"width":360,"height":780,"logicalDpi":96,"logicalBaseDpi":96,"dpr":1}]})");config.close();}
    qputenv("QT_QPA_PLATFORM",("offscreen:configfile="+config.fileName()).toUtf8());
    for(const auto &[key,value]:{std::pair{"QT_QUICK_BACKEND","software"},{"QT_QUICK_CONTROLS_STYLE","Basic"},{"QT_QPA_PLATFORMTHEME","generic"},{"QT_FORCE_STDERR_LOGGING","1"}})qputenv(key,value);
    QtWebEngineQuick::initialize();QGuiApplication app(argc,argv);
    app.setOrganizationName("tomacco-phone-tests");app.setApplicationName("Tomacco");app.setApplicationVersion("9.8.7-test");
    qmlRegisterType<NativeView>("Tomacco.Native",1,0,"NativeView");
    PhoneRoutesTest test;return QTest::qExec(&test,argc,argv);
}
#include "phone_routes_test.moc"
