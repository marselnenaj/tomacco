#include "backend.h"
#include "control.h"
#include "nativeview.h"
#include <QGuiApplication>
#include <QInputMethodEvent>
#include <QDateTime>
#include <QJSValue>
#include <QPainter>
#include <QQmlApplicationEngine>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlProperty>
#include <QQuickItem>
#include <QQuickWindow>
#include <QScopeGuard>
#include <QSettings>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QFile>
#include <QBuffer>
#include <QJsonDocument>
#include <QLocalServer>
#include <QLocalSocket>
#include <QMimeData>
#include <QClipboard>
#include <QImage>
#include <QtEndian>
#include <QtMath>
#include <QUrl>
#include <QtTest>
#include <QAccessible>
#include <QtWebEngineQuick/qtwebenginequickglobal.h>
#include <QThread>
#include <QTcpServer>
#include <QTcpSocket>
#include <QElapsedTimer>
#include <QProcess>
#include <QJsonArray>
#include <QCryptographicHash>
#include <unistd.h>
#include <algorithm>
#include <cmath>
#include <functional>
#include <utility>

// The tests' own bounds for the backend, its workers and the scene, stretched
// on a loaded host as the Python suite's are (tests/host_patience.py):
// TOMACCO_TEST_PATIENCE when set (a whole number of at least 1), otherwise the
// 1-minute load per CPU rounded up (1 while the load is at most the CPU count,
// so a quiet host runs the test unchanged), at most 8.
static int patience() {
    static const int factor=[]{
        bool ok=false;const int pinned=qEnvironmentVariableIntValue("TOMACCO_TEST_PATIENCE",&ok);
        if(ok)return std::max(1,pinned);
        QFile loadavg("/proc/loadavg");
        if(!loadavg.open(QIODevice::ReadOnly))return 1;
        const double load=loadavg.readLine().split(' ').value(0).toDouble();
        const int cpus=std::max(1,QThread::idealThreadCount());
        return load<=cpus?1:std::min(8,int(std::ceil(load/cpus)));
    }();
    return factor;
}
static int patient(int milliseconds) { return milliseconds*patience(); }

// The navigation rail and its border, which an open panel leaves beside it.
static constexpr int Theme_railPlusBorder=60;
// Half a checkbox's indicator: a click there toggles it.
static constexpr int Theme_iconHalf=8;

class DesktopUiTest : public QObject {
    Q_OBJECT
    QTemporaryDir directory;
    std::unique_ptr<Backend> backend;
    std::unique_ptr<Control> control;
    std::unique_ptr<QQmlApplicationEngine> engine;
    QQuickWindow *window=nullptr;
    QObject *workspace=nullptr;
    QStringList qmlErrors;
    int quits=0;

    QObject *object(const char *name) { return window->findChild<QObject *>(QString::fromLatin1(name)); }
    QQuickItem *visualItem(QQuickItem *parent,const QString &name) {
        if(parent->objectName()==name)return parent;
        for(auto *child:parent->childItems())if(auto *found=visualItem(child,name))return found;
        return nullptr;
    }
    QQuickItem *item(const char *name) { return visualItem(window->contentItem(),QString::fromLatin1(name)); }
    void type(const QString &text) {
        QInputMethodEvent event;
        event.setCommitString(text);
        QCoreApplication::sendEvent(QGuiApplication::focusObject(),&event);
    }
    void open(const QString &tab,const QString &file={}) {
        QVERIFY(QMetaObject::invokeMethod(window,"showWorkspace",Q_ARG(QVariant,tab),Q_ARG(QVariant,directory.path()+"/project"),Q_ARG(QVariant,file),Q_ARG(QVariant,1)));
    }
    void capture(const QString &name) {
        const auto out=qEnvironmentVariable("TOMACCO_UI_CAPTURES");
        if(out.isEmpty())return;
        QVERIFY(QDir().mkpath(QFileInfo(out+"/"+name).absolutePath()));
        QTest::mouseMove(window,QPoint(window->width()-8,8));
        QTest::qWait(150);
        QVERIFY(window->grabWindow().save(out+"/"+name+".png"));
    }
    // What assistive technology reads for an item.
    static QString accessibleName(QQuickItem *item) {
        auto *face=QAccessible::queryAccessibleInterface(item);
        return face?face->text(QAccessible::Name):QString();
    }
    // One request on the preview socket, as tomaccoctl sends it: {result} or {error}.
    QVariantMap previewCall(const QString &method,const QVariantMap &params={}) {
        // Where Control puts it: the data directory's runtime/, or /tmp when that path is too long.
        const auto data=QDir::cleanPath(QFileInfo(Backend::dataDirectory()).absoluteFilePath());
        auto runtime=data+"/runtime";
        if(runtime.toUtf8().size()+54>=104)runtime=QStringLiteral("/tmp/tomacco-%1-%2").arg(getuid()).arg(QString::fromLatin1(QCryptographicHash::hash(data.toUtf8(),QCryptographicHash::Sha256).toHex().left(16)));
        QLocalSocket socket;socket.connectToServer(runtime+"/preview-ui-acceptance.sock");
        if(!socket.waitForConnected(2000))return {{"error","not connected"}};
        socket.write(QJsonDocument(QJsonObject{{"id",1},{"method",method},{"params",QJsonObject::fromVariantMap(params)}}).toJson(QJsonDocument::Compact)+"\n");socket.flush();
        QElapsedTimer clock;clock.start();
        while(!socket.canReadLine() && clock.elapsed()<patient(30000))QTest::qWait(10);
        return QJsonDocument::fromJson(socket.readLine()).object().toVariantMap();
    }
    // A web server on 127.0.0.1 for the layout lab: a page at /, two bytes anywhere else.
    struct PageServer {
        QTcpServer server;
        explicit PageServer(const QByteArray &page) {
            QObject::connect(&server,&QTcpServer::newConnection,&server,[this,page]{
                while(auto *socket=server.nextPendingConnection()) {
                    QObject::connect(socket,&QTcpSocket::readyRead,socket,[socket,page]{
                        const auto request=socket->readAll();if(!request.contains("\r\n\r\n"))return;
                        const auto path=request.split(' ').value(1);
                        const QByteArray body=path=="/"?page:path.startsWith("/missing")?QByteArray("gone"):QByteArray("ok");
                        const QByteArray status=path.startsWith("/missing")?"404 Not Found":"200 OK";
                        socket->write("HTTP/1.1 "+status+"\r\nContent-Type: "+(path=="/"?"text/html":"text/plain")+"\r\nCache-Control: no-store\r\nContent-Length: "+QByteArray::number(body.size())+"\r\nConnection: close\r\n\r\n"+body);
                        socket->disconnectFromHost();
                    });
                    QObject::connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
                }
            });
            server.listen(QHostAddress::LocalHost);
        }
        QString url(const QString &path="/") const { return QString("http://127.0.0.1:%1%2").arg(server.serverPort()).arg(path); }
    };
    // Navigates the lab and waits until the page has loaded.
    bool openPage(const QString &url) {
        if(previewCall("navigate",{{"url",url}}).contains("error"))return false;
        QElapsedTimer clock;clock.start();
        while(clock.elapsed()<patient(15000)) {
            const auto state=previewCall("state").value("result").toMap();
            if(state.value("url").toString()==url && !state.value("loading").toBool())return true;
            QTest::qWait(50);
        }
        return false;
    }
    QVariant evaluate(const QString &script) { return previewCall("evaluate",{{"script",script}}).value("result"); }
    // A var property holds a JS value or, once set from C++ or from a reply, a plain map.
    static QVariantMap mapOf(const QVariant &value) {
        return value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant().toMap():value.toMap();
    }
    // A stand-in adb for this test run's control plane, a shell script with
    // this body; the guard it returns removes it from the settings again.
    auto useFakeAdb(const QString &body) {
        const auto fake=directory.filePath("fake-adb");
        {QFile script(fake);script.open(QIODevice::WriteOnly|QIODevice::Truncate);
         script.write(("#!/bin/sh\nd='"+directory.path()+"'\n"+body+"\nexit 0\n").toUtf8());script.close();
         script.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner|QFileDevice::ExeOwner);}
        QSignalSpy replies(control.get(),&Control::apiFinished);
        const int id=control->request("settings.configure",{{"executables",QVariantMap{{"adb",fake}}}});
        QElapsedTimer clock;clock.start();
        while(!std::any_of(replies.begin(),replies.end(),[&](const QList<QVariant> &r){return r.value(0).toInt()==id;}) && clock.elapsed()<patient(10000))QTest::qWait(20);
        return qScopeGuard([this]{
            QFile file(directory.filePath("state/settings.json"));
            if(!file.open(QIODevice::ReadOnly))return;
            auto value=QJsonDocument::fromJson(file.readAll()).object();file.close();
            auto tools=value.value("executables").toObject();tools.remove("adb");value["executables"]=tools;
            if(file.open(QIODevice::WriteOnly|QIODevice::Truncate))file.write(QJsonDocument(value).toJson());
        });
    }
private slots:
    void initTestCase() {
        QVERIFY(directory.isValid());
        qputenv("TOMACCO_HOME",directory.filePath("state").toUtf8());
        qputenv("TOMACCO_UPDATES","off");
        qputenv("XDG_CONFIG_HOME",directory.filePath("config").toUtf8());
        qputenv("XDG_CACHE_HOME",directory.filePath("cache").toUtf8());
        QVERIFY(QDir().mkpath(directory.filePath("project")));
        QFile source(directory.filePath("project/pubspec.yaml"));QVERIFY(source.open(QIODevice::WriteOnly));source.write("name: tomacco_ui_test\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n");source.close();
        // This version has started here before: no sheet for a first start or a new version.
        QSettings().setValue("app/lastVersion",QCoreApplication::applicationVersion());
        backend=std::make_unique<Backend>(QString());
        Backend::applyInterfaceFont();
        // The checks below read the English interface whatever the host's
        // language is; german_interface_switches_live covers the German one.
        qunsetenv("TOMACCO_LANGUAGE");
        QCOMPARE(backend->language(),QString("system"));
        backend->setLanguage("en");
        QCOMPARE(backend->uiLanguage(),QString("en"));
        control=std::make_unique<Control>("ui-acceptance");
        engine=std::make_unique<QQmlApplicationEngine>();
        // As in main.cpp: a language chosen in the settings applies at once.
        connect(backend.get(),&Backend::languageChanged,engine.get(),[this]{engine->setUiLanguage(backend->uiLanguage());engine->retranslate();});
        connect(engine.get(),&QQmlEngine::warnings,this,[this](const QList<QQmlError> &errors) {
            for(const auto &error:errors)if(error.url().toString().startsWith("qrc:/qml/"))qmlErrors.append(error.toString());
        });
        // Qt.quit() would end the shared test window; count the requests instead.
        disconnect(engine.get(),&QQmlEngine::quit,nullptr,nullptr);
        connect(engine.get(),&QQmlEngine::quit,this,[this]{++quits;});
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
        workspace=object("workspacePanel");QVERIFY(workspace);
        QTRY_VERIFY_WITH_TIMEOUT(control->operations().size()>100,patient(10000));
        QTRY_VERIFY_WITH_TIMEOUT(!workspace->property("resultText").toString().isEmpty(),patient(10000));
        const auto project=workspace->property("project");
        const auto projectMap=project.metaType()==QMetaType::fromType<QJSValue>()?project.value<QJSValue>().toVariant().toMap():project.toMap();
        QCOMPARE(projectMap.value("path").toString(),directory.filePath("project"));
    }
    void command_palette_keyboard_and_focus() {
        QTest::keyClick(window,Qt::Key_K,Qt::ControlModifier);
        auto *palette=object("commandPalette");QVERIFY(palette);
        QTRY_VERIFY(palette->property("visible").toBool());
        QTRY_VERIFY(item("commandSearch")->hasActiveFocus());
        type("Files");QTest::keyClick(window,Qt::Key_Return);
        QTRY_VERIFY(workspace->property("opened").toBool());
        QCOMPARE(workspace->property("section").toString(),QString("files"));
        QVERIFY(!palette->property("visible").toBool());
        QTest::keyClick(window,Qt::Key_K,Qt::ControlModifier);QTRY_VERIFY(palette->property("visible").toBool());
        type("files.read");capture("command-palette");
        QTest::keyClick(window,Qt::Key_Return);
        QTRY_COMPARE(workspace->property("section").toString(),QString("tools"));
        const auto value=workspace->property("selectedOperation");
        const auto operation=value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant().toMap():value.toMap();
        QCOMPARE(operation.value("name").toString(),QString("files.read"));
        // Selecting a command opens its form. It must not execute automatically.
        QVERIFY(!workspace->property("status").toString().contains("Running"));
        QTest::keyClick(window,Qt::Key_K,Qt::ControlModifier);QTRY_VERIFY(palette->property("visible").toBool());
        type("no-such-studio-operation");QTest::keyClick(window,Qt::Key_Return);
        QVERIFY(palette->property("visible").toBool());
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!palette->property("visible").toBool());
    }
    void file_edits_survive_navigation_and_save() {
        open("files","pubspec.yaml");
        auto *editor=item("sourceEditor");QVERIFY(editor);
        QTRY_VERIFY_WITH_TIMEOUT(editor->property("text").toString().contains("name: tomacco_ui_test"),patient(10000));
        editor->forceActiveFocus();QTest::keyClick(window,Qt::Key_End,Qt::ControlModifier);type("# retained edit\n");
        QVERIFY(editor->property("text").toString().endsWith("# retained edit\n"));
        auto *nav=item("nav-runtime");QVERIFY(nav);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,nav->mapToScene(QPointF(nav->width()/2,nav->height()/2)).toPoint());
        QTRY_VERIFY(!workspace->property("opened").toBool());
        QTest::keyClick(window,Qt::Key_2,Qt::ControlModifier);
        QTRY_COMPARE(workspace->property("section").toString(),QString("files"));
        QTRY_VERIFY(workspace->property("opened").toBool());
        QCOMPARE(editor->property("text").toString().count("# retained edit"),1);
        QTest::keyClick(window,Qt::Key_S,Qt::ControlModifier);
        QTRY_VERIFY_WITH_TIMEOUT(([&]{QFile file(directory.filePath("project/pubspec.yaml"));return file.open(QIODevice::ReadOnly) && file.readAll().endsWith("# retained edit\n");})(),patient(10000));
        capture("files");
    }
    void all_pages_at_minimum_and_normal_size() {
        for(const auto &size:{QSize(960,680),QSize(1380,970)}) {
            window->resize(size);
            for(const auto &tab:{"runtime","project","devices","jobs","tools","debug","testing","profiling","files","release"}) {
                open(QString::fromLatin1(tab));QTest::qWait(100);
                auto *nav=item((QByteArray("nav-")+tab).constData());QVERIFY(nav);
                const auto bounds=nav->mapRectToScene(nav->boundingRect());
                QVERIFY2(bounds.left()>=0 && bounds.right()<=window->width(),tab);
                QVERIFY2(bounds.top()>=44 && bounds.bottom()<window->height()-95,tab);
                capture(QString::fromLatin1(tab)+"-"+QString::number(size.width()));
            }
        }
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void project_scroll_works_at_minimum_size() {
        window->resize(960,680);open("project");QTest::qWait(100);
        auto *scroll=item("projectOverview");QVERIFY(scroll);
        auto *flickable=qobject_cast<QQuickItem *>(scroll->property("contentItem").value<QObject *>());QVERIFY(flickable);
        flickable->setProperty("contentY",0);
        const auto point=scroll->mapToScene(QPointF(scroll->width()/2,scroll->height()/2));
        QWheelEvent wheel(point,QPointF(window->mapToGlobal(point.toPoint())),{},QPoint(0,-720),Qt::NoButton,Qt::NoModifier,Qt::NoScrollPhase,false);
        QCoreApplication::sendEvent(window,&wheel);
        QTRY_VERIFY(flickable->property("contentY").toReal()>100);
        capture("project-scrolled-960");
        flickable->setProperty("contentY",0);
    }
    void native_debug_setup_validates_target_and_prevents_duplicate_requests() {
        open("debug");
        QTRY_VERIFY_WITH_TIMEOUT(([&]{
            const auto value=workspace->property("pending");
            const auto pending=value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant().toMap():value.toMap();
            for(const auto &request:pending)if(request.toMap().value("operation")=="device.list")return false;
            return true;
        })(),patient(10000));
        auto *debug=object("debugWorkspace");QVERIFY(debug);
        auto *picker=object("debugBackend");QVERIFY(picker);QCOMPARE(picker->property("count").toInt(),3);
        auto *application=object("debugApplication");auto *program=object("debugProgram");auto *start=item("debugStart");
        QVERIFY(application);QVERIFY(program);QVERIFY(start);
        const auto devices=workspace->property("devices");
        const auto selected=workspace->property("selectedDevice");const auto platform=workspace->property("selectedPlatform");
        workspace->setProperty("devices",QVariantList{QVariantMap{{"id","debug-ui-device"},{"platform","android"},{"name","Android debug target"}}});
        workspace->setProperty("selectedDevice","debug-ui-device");workspace->setProperty("selectedPlatform","ios");
        picker->setProperty("currentIndex",1);application->setProperty("text","dev.example.app");
        QVERIFY(!start->isEnabled());
        workspace->setProperty("selectedPlatform","android");QVERIFY(!start->isEnabled());
        QFile invalid(directory.filePath("invalid-library.so"));QVERIFY(invalid.open(QIODevice::WriteOnly));invalid.write(QByteArray(64,'x'));invalid.close();
        program->setProperty("text",invalid.fileName());QVERIFY(start->isEnabled());
        for(const auto &size:{QSize(960,680),QSize(1380,970)}) {
            window->resize(size);QTest::qWait(100);
            const auto bounds=start->mapRectToScene(start->boundingRect());
            QVERIFY(bounds.left()>=0 && bounds.right()<=window->width());
            QVERIFY(bounds.top()>=44 && bounds.bottom()<window->height());
            capture("android-native-debug-"+QString::number(size.width()));
        }
        QSignalSpy replies(control.get(),&Control::apiFinished);
        QVERIFY(QMetaObject::invokeMethod(debug,"start"));
        QVERIFY(debug->property("starting").toBool());QVERIFY(!start->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(debug,"start"));
        QTRY_VERIFY_WITH_TIMEOUT(!debug->property("starting").toBool(),patient(5000));
        QVERIFY(!debug->property("failure").toString().isEmpty());
        int starts=0;for(const auto &reply:replies)if(reply[1].toString()=="debug.start")++starts;
        QCOMPARE(starts,1);QVERIFY(start->isEnabled());
        // Validation failed before contacting the deliberately nonexistent device.
        QVERIFY(debug->property("failure").toString().contains("ELF"));
        program->setProperty("text","");picker->setProperty("currentIndex",0);debug->setProperty("failure","");
        workspace->setProperty("devices",devices);workspace->setProperty("selectedDevice",selected);workspace->setProperty("selectedPlatform",platform);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void native_audio_controls_follow_session_and_ignore_stale_replies() {
        QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
        auto *view=object("nativeWorkspace");auto *toggle=item("nativeAudioToggle");auto *log=item("nativeAudioLog");
        QVERIFY(view);QVERIFY(toggle);QVERIFY(log);
        view->setProperty("autoAttach",false);
        QVariantMap session{{"session","audio-ui-current"},{"platform","android"},{"status","connected"},{"device","audio-ui-device"},
                            {"audio",QVariantMap{{"enabled",true},{"configured",true},{"status","running"},{"job_id","audio-ui-job"}}}};
        view->setProperty("live",session);
        QVERIFY(toggle->isVisible());QVERIFY(toggle->isEnabled());QVERIFY(toggle->property("selected").toBool());QVERIFY(!log->isVisible());
        for(const auto &size:{QSize(960,680),QSize(1380,970)}) {
            window->resize(size);QTest::qWait(50);
            const auto bounds=toggle->mapRectToScene(toggle->boundingRect());
            QVERIFY(bounds.left()>=0 && bounds.right()<=window->width());
            capture("android-audio-"+QString::number(size.width()));
        }
        view->setProperty("audioPendingSession","audio-ui-current");QVERIFY(!toggle->isEnabled());
        const auto pending=[](const QString &id){return QVariantMap{{"op","runtime.audio"},{"session",id}};};
        view->setProperty("requests",QVariantMap{{"-41",pending("audio-ui-old")},{"-42",pending("audio-ui-current")}});
        control->apiFinished(-41,"runtime.audio",QVariantMap{{"ok",true},{"result",QVariantMap{{"enabled",false},{"status","off"}}}});
        QVERIFY(toggle->property("selected").toBool());QVERIFY(!toggle->isEnabled());
        control->apiFinished(-42,"runtime.audio",QVariantMap{{"ok",true},{"result",QVariantMap{{"enabled",false},{"status","failed"},{"error","Audio capture failed"},{"job_id","audio-ui-job"}}}});
        QVERIFY(!toggle->property("selected").toBool());QVERIFY(toggle->isEnabled());QVERIFY(log->isVisible());
        QCOMPARE(toggle->property("text").toString(),QString("Sound error"));
        session["platform"]="ios";session.remove("audio");view->setProperty("live",session);
        QVERIFY(!toggle->isVisible());QVERIFY(!log->isVisible());
        view->setProperty("live",QVariantMap{});view->setProperty("audioPendingSession",QString());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void native_frames_preserve_real_pixels_and_pointer_coordinates() {
        open("runtime");window->resize(1380,970);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *display=qobject_cast<NativeView *>(object("nativeDisplay"));QVERIFY(display);
        auto *frame=item("nativeDeviceFrame");QVERIFY(frame);
        auto *toggle=item("nativeFrameToggle");QVERIFY(toggle);
        view->setProperty("autoAttach",false);view->setProperty("showDeviceFrame",true);
        QLocalServer video,controlSocket;
        QVERIFY(video.listen(directory.filePath("frame-video")));
        QVERIFY(controlSocket.listen(directory.filePath("frame-input")));
        QByteArray packet;QVector<QJsonObject> inputs;
        connect(&video,&QLocalServer::newConnection,this,[&]{auto *socket=video.nextPendingConnection();socket->write(packet);connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        connect(&controlSocket,&QLocalServer::newConnection,this,[&]{auto *socket=controlSocket.nextPendingConnection();connect(socket,&QLocalSocket::readyRead,socket,[&,socket]{if(!socket->canReadLine())return;inputs.push_back(QJsonDocument::fromJson(socket->readLine()).object()["arguments"].toObject());socket->write("{\"ok\":true}\n");});connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        const auto start=[&](const QVariantMap &profile,QSize size,const QString &id,bool assigned){
            QImage image(size,QImage::Format_RGB32);image.fill(QColor("#295c83"));QByteArray bytes;QBuffer output(&bytes);output.open(QIODevice::WriteOnly);image.save(&output,"JPEG");packet=QByteArray(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;
            QVariantMap target{{"id",id},{"name","Frame test"},{"platform",profile["platform"]},{"kind",id.startsWith("emulator-")?"emulator":"device"}};
            if(assigned){target["profile_id"]=profile["id"];target["profile_source"]="assigned";}
            view->setProperty("targets",QVariantList{target});
            QVariantMap session{{"session",profile["id"].toString()+QString::number(size.width())+"x"+QString::number(size.height())+id},{"platform",profile["platform"]},{"device",id},{"codec","mjpeg"},{"video_socket",video.fullServerName()},{"control_socket",controlSocket.fullServerName()}};
            QVERIFY(QMetaObject::invokeMethod(view,"attach",Q_ARG(QVariant,session)));
            QTRY_COMPARE_WITH_TIMEOUT(display->sourceSize(),size,patient(3000));
        };
        for(const auto &entry:backend->models()) {
            const auto profile=entry.toMap();if(profile["platform"]!="android")continue;
            const auto size=QSize(profile["width"].toInt(),profile["height"].toInt());
            start(profile,size,"emulator-frame",true);
            QVERIFY(frame->property("framed").toBool());
            const auto actual=frame->property("profile");QCOMPARE((actual.metaType()==QMetaType::fromType<QJSValue>()?actual.value<QJSValue>().toVariant().toMap():actual.toMap()).value("id"),profile["id"]);
            QVERIFY(qAbs(display->contentRect().width()/display->contentRect().height()-double(size.width())/size.height())<.0001);
            inputs.clear();QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,display->mapToScene(display->contentRect().center()).toPoint());
            QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),2,patient(3000));
            QVERIFY(qAbs(inputs[0]["x"].toDouble()-.5)<.005);QVERIFY(qAbs(inputs[0]["y"].toDouble()-.5)<.005);
            // Rounded corners and camera holes are hardware, not app touch areas (a tablet has no hole).
            if(profile.value("cutout").toString()!="none") {
                auto *camera=item("nativeCameraCutout");QVERIFY(camera);
                QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,camera->mapToScene(QPointF(camera->width()/2,camera->height()/2)).toPoint());
                QTest::qWait(30);QCOMPARE(inputs.size(),2);
            }
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,display->mapToScene(QPointF(1,1)).toPoint());QTest::qWait(30);QCOMPARE(inputs.size(),2);
            capture("frame-"+profile["id"].toString());
            start(profile,QSize(size.height(),size.width()),"emulator-frame",true);
            QVERIFY(frame->property("landscape").toBool());QVERIFY(frame->property("framed").toBool());
            if(profile.contains("openWidth")){start(profile,QSize(profile["openWidth"].toInt(),profile["openHeight"].toInt()),"emulator-frame",true);QVERIFY(frame->property("unfolded").toBool());capture("frame-fold-open");}
        }
        QVariantMap profile;for(const auto &p:backend->models())if(p.toMap()["id"]=="pixel-11")profile=p.toMap();
        start(profile,QSize(1080,2424),"emulator-frame",false);QVERIFY(frame->property("framed").toBool());
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,toggle->mapToScene(QPointF(toggle->width()/2,toggle->height()/2)).toPoint());
        QVERIFY(!frame->property("framed").toBool());QCOMPARE(display->sourceSize(),QSize(1080,2424));
        view->setProperty("showDeviceFrame",true);
        // Detaching moves this same display into the device window: one
        // stream, correct pointer coordinates, and a clean return.
        QVERIFY(frame->property("framed").toBool());
        const auto frameCount=display->frameCount();
        view->setProperty("detached",true);
        QTRY_VERIFY_WITH_TIMEOUT(display->window() && display->window()!=window,patient(3000));
        auto *deviceWindow=display->window();
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isVisible(),patient(3000));
        QVERIFY(deviceWindow->flags().testFlag(Qt::FramelessWindowHint));
        QCOMPARE(deviceWindow->color(),QColor(Qt::transparent));
        QVERIFY(deviceWindow->width()<window->width());
        QTRY_VERIFY_WITH_TIMEOUT(display->contentRect().width()>0,patient(3000));
        QVERIFY(qAbs(display->contentRect().width()/display->contentRect().height()-1080.0/2424)<.0001);
        QCOMPARE(display->sourceSize(),QSize(1080,2424));QVERIFY(display->frameCount()>=frameCount);
        inputs.clear();QTest::mouseClick(deviceWindow,Qt::LeftButton,Qt::NoModifier,display->mapToScene(display->contentRect().center()).toPoint());
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),2,patient(3000));
        QVERIFY(qAbs(inputs[0]["x"].toDouble()-.5)<.005);QVERIFY(qAbs(inputs[0]["y"].toDouble()-.5)<.005);
        // Device-only: the main window hides while the device is detached and
        // returns, with the device docked, when the workspace is requested.
        view->setProperty("solo",true);
        QTRY_VERIFY_WITH_TIMEOUT(!window->isVisible(),patient(3000));
        QVERIFY(deviceWindow->isVisible());QVERIFY(deviceWindow->property("solo").toBool());
        QVERIFY(QMetaObject::invokeMethod(deviceWindow,"dockRequested"));
        QTRY_VERIFY_WITH_TIMEOUT(window->isVisible(),patient(3000));
        QVERIFY(!view->property("solo").toBool());
        QTRY_COMPARE_WITH_TIMEOUT(display->window(),window,patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(!deviceWindow->isVisible(),patient(3000));
        // Ctrl+wheel and Ctrl+0 resize the device window instead of reaching
        // the device, also once a click has given the device view the keyboard.
        // Escape still belongs to the focused device.
        view->setProperty("detached",true);
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isVisible(),patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(display->window()==deviceWindow && display->contentRect().width()>0,patient(3000));
        deviceWindow->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isActive(),patient(3000));
        auto centre=display->mapToScene(display->contentRect().center());
        inputs.clear();QTest::mouseClick(deviceWindow,Qt::LeftButton,Qt::NoModifier,centre.toPoint());
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),2,patient(3000));QVERIFY(display->hasActiveFocus());inputs.clear();
        const auto zoom=deviceWindow->property("zoom").toReal();
        QWheelEvent shrink(centre,QPointF(deviceWindow->mapToGlobal(centre.toPoint())),{},QPoint(0,-120),Qt::NoButton,Qt::ControlModifier,Qt::NoScrollPhase,false);
        QCoreApplication::sendEvent(deviceWindow,&shrink);
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->property("zoom").toReal()<zoom,patient(3000));
        const auto smaller=deviceWindow->property("zoom").toReal();
        QTest::keyClick(deviceWindow,Qt::Key_Minus,Qt::ControlModifier);
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->property("zoom").toReal()<smaller,patient(3000));
        QTest::keyClick(deviceWindow,Qt::Key_0,Qt::ControlModifier);
        // Zoom moves in steps of five percent and never beyond what fits.
        const auto fit=deviceWindow->property("fitZoom").toReal();
        QTRY_COMPARE_WITH_TIMEOUT(deviceWindow->property("zoom").toReal(),qMin(fit,std::round(qMin(1.0,fit)*20)/20),patient(3000));
        QTest::qWait(250);QCOMPARE(inputs.size(),0);
        QTest::keyClick(deviceWindow,Qt::Key_Escape);
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),1,patient(3000));QCOMPARE(inputs[0]["key"].toString(),QString("escape"));
        QVERIFY(view->property("detached").toBool());QVERIFY(deviceWindow->isVisible());
        view->setProperty("detached",false);
        QTRY_COMPARE_WITH_TIMEOUT(display->window(),window,patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(!deviceWindow->isVisible(),patient(3000));
        window->requestActivate();
        // A starting emulator already shows its frame and a startup screen.
        view->setProperty("live",QVariantMap{});display->setSession({});
        const QVariant booting=QVariantMap{{"pending_avd","Boot test"},{"profile_id","pixel-11"},{"detached",true}};
        const QVariant booted=QVariantMap{{"pending_avd",""},{"detached",false}};
        QVERIFY(QMetaObject::invokeMethod(view,"configure",Q_ARG(QVariant,booting)));
        // The frame has moved to the device window; look it up by object tree.
        auto *bootScreen=qobject_cast<QQuickItem *>(object("nativeBootScreen"));QVERIFY(bootScreen);
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isVisible(),patient(3000));
        QVERIFY(bootScreen->isVisible());QVERIFY(frame->property("framed").toBool());
        QCOMPARE(bootScreen->window(),deviceWindow);
        QVERIFY(!deviceWindow->property("controlsEnabled").toBool());
        QVERIFY(QMetaObject::invokeMethod(view,"configure",Q_ARG(QVariant,booted)));
        QTRY_VERIFY_WITH_TIMEOUT(!deviceWindow->isVisible(),patient(3000));QVERIFY(!bootScreen->isVisible());
        start(profile,QSize(1080,2424),"emulator-frame",false);QVERIFY(frame->property("framed").toBool());
        start(profile,QSize(1080,1920),"emulator-frame",true);QVERIFY(!frame->property("framed").toBool());
        start(profile,QSize(1080,2424),"physical-unknown",false);QVERIFY(!frame->property("framed").toBool());
        view->setProperty("live",QVariantMap{});display->setSession({});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void device_window_lifecycle_keeps_edits_and_never_strands_the_app() {
        QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *deviceWindow=qobject_cast<QQuickWindow *>(object("deviceWindow"));QVERIFY(deviceWindow);
        auto *files=object("fileWorkspace");QVERIFY(files);
        auto *prompt=object("unsavedCloseDialog");QVERIFY(prompt);
        const auto configure=[&](const QVariantMap &args){QVERIFY(QMetaObject::invokeMethod(view,"configure",Q_ARG(QVariant,QVariant(args))));};
        // A var property holds a JS value or, once set from C++ or from a reply, a plain map.
        const auto map=[&](const char *name){const auto value=view->property(name);return value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant().toMap():value.toMap();};
        const auto reply=[&](int id,const QString &op,const QVariant &result){
            auto requests=map("requests");
            requests[QString::number(id)]=QVariantMap{{"op",op},{"quiet",true}};
            view->setProperty("requests",requests);
            control->apiFinished(id,op,QVariantMap{{"ok",true},{"result",result}});
        };
        const auto unsaved=[&]{QVariant value;QMetaObject::invokeMethod(workspace,"hasUnsaved",Q_RETURN_ARG(QVariant,value));return value.toBool();};
        view->setProperty("live",QVariantMap{});quits=0;
        // Device-only needs a device window that exists or is on its way.
        configure({{"detached",true},{"solo",true}});
        QVERIFY(!view->property("solo").toBool());QVERIFY(window->isVisible());QVERIFY(!deviceWindow->isVisible());
        // While one device is awaited, no other session may appear under its
        // startup screen; its own session is attached as soon as it exists.
        configure({{"pending_avd","Lifecycle"},{"profile_id","pixel-11"},{"detached",true}});
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isVisible(),patient(3000));
        const QVariantMap other{{"session","other-session"},{"platform","android"},{"status","connected"},{"device","emulator-other"}};
        const QVariantMap own{{"session","lifecycle-session"},{"platform","android"},{"status","connected"},{"device","emulator-lifecycle"}};
        const QVariantMap booted{{"id","emulator-lifecycle"},{"name","Lifecycle"},{"avd","Lifecycle"},{"platform","android"},{"kind","emulator"},{"state","device"},{"booted",true}};
        reply(-61,"runtime.list",QVariantList{other});
        reply(-62,"device.list",QVariantMap{{"devices",QVariantList{QVariantMap{{"id","emulator-other"},{"name","Other"},{"avd","Other"},{"platform","android"},{"kind","emulator"},{"state","device"},{"booted",true}}}}});
        QVERIFY(map("live").value("session").toString().isEmpty());
        QVERIFY(view->property("pending").toBool());QVERIFY(view->property("pendingTarget").toString().isEmpty());
        reply(-63,"runtime.list",QVariantList{other,own});
        reply(-64,"device.list",QVariantMap{{"devices",QVariantList{booted}}});
        QCOMPARE(view->property("pendingTarget").toString(),QString("emulator-lifecycle"));
        QCOMPARE(map("live").value("session").toString(),QString("lifecycle-session"));
        QVERIFY(view->property("pending").toBool());QVERIFY(deviceWindow->isVisible());
        // A known device that is being connected gets a startup frame of its
        // own platform and, where its model is known, of that model.
        const auto frameProfile=[&]{const auto value=object("nativeDeviceFrame")->property("profile");return value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant().toMap():value.toMap();};
        view->setProperty("live",QVariantMap{});
        view->setProperty("targets",QVariantList{
            QVariantMap{{"id","iphone-await"},{"name","Await iPhone"},{"platform","ios"},{"kind","device"}},
            QVariantMap{{"id","pixel-await"},{"name","Await Pixel"},{"platform","android"},{"kind","device"},{"profile_id","pixel-11"}}});
        configure({{"platform","ios"},{"device","iphone-await"},{"connect",true},{"detached",true}});
        QVERIFY(view->property("pending").toBool());QCOMPARE(view->property("pendingLabel").toString(),QString("Await iPhone"));
        QCOMPARE(frameProfile().value("platform").toString(),QString("ios"));QCOMPARE(frameProfile().value("cutout").toString(),QString("island"));
        configure({{"platform","android"},{"device","pixel-await"},{"connect",true},{"detached",true}});
        QCOMPARE(view->property("pendingDevice").toString(),QString("pixel-await"));
        QCOMPARE(frameProfile().value("id").toString(),QString("pixel-11"));
        configure({{"pending_avd","Lifecycle"},{"profile_id","pixel-11"},{"detached",true}});
        reply(-66,"runtime.list",QVariantList{other,own});
        reply(-67,"device.list",QVariantMap{{"devices",QVariantList{booted}}});
        QCOMPARE(map("live").value("session").toString(),QString("lifecycle-session"));
        // Closing the main window continues as the device window. Unsaved
        // edits are neither asked about nor dropped.
        QVERIFY(QMetaObject::invokeMethod(files,"newFile",Q_ARG(QVariant,QString("lifecycle-draft.txt"))));
        QVERIFY(unsaved());
        window->close();
        QTRY_VERIFY_WITH_TIMEOUT(!window->isVisible(),patient(3000));
        QVERIFY(view->property("solo").toBool());QVERIFY(deviceWindow->isVisible());
        QVERIFY(!prompt->property("visible").toBool());QVERIFY(!window->property("discardOnClose").toBool());QVERIFY(unsaved());
        // Closing the device window would end the app. With unsaved edits the
        // device docks into the returning main window and its session stays.
        deviceWindow->close();
        QTRY_VERIFY_WITH_TIMEOUT(window->isVisible(),patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(!deviceWindow->isVisible(),patient(3000));
        QVERIFY(!view->property("solo").toBool());QVERIFY(!view->property("quitting").toBool());QCOMPARE(quits,0);
        QCOMPARE(map("live").value("session").toString(),QString("lifecycle-session"));
        // A late answer to a stop that was sent for an abandoned quit ends nothing.
        auto requests=map("requests");
        requests["-65"]=QVariantMap{{"op","runtime.stop"},{"quit",true},{"quiet",true}};view->setProperty("requests",requests);
        control->apiFinished(-65,"runtime.stop",QVariantMap{{"ok",true},{"result",QVariantMap{}}});
        QCOMPARE(quits,0);QVERIFY(!view->property("quitting").toBool());
        // Without a device window the main window asks before it closes.
        window->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(window->isActive(),patient(3000));
        window->close();
        QVERIFY(window->isVisible());QTRY_VERIFY_WITH_TIMEOUT(prompt->property("visible").toBool(),patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(prompt->property("opened").toBool(),patient(3000));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY_WITH_TIMEOUT(!prompt->property("visible").toBool(),patient(3000));
        QVERIFY(window->isVisible());QVERIFY(unsaved());QCOMPARE(quits,0);
        // Tabs first: every tab reads its own document.
        files->setProperty("tabs",QVariantList{});files->setProperty("current",QString());files->setProperty("documents",QVariantMap{});
        QVERIFY(!unsaved());
        // With nothing unsaved, closing the device-only window ends the app once.
        view->setProperty("live",QVariantMap{});
        configure({{"pending_avd","Lifecycle"},{"profile_id","pixel-11"},{"detached",true},{"solo",true}});
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isVisible(),patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(!window->isVisible(),patient(3000));QVERIFY(view->property("solo").toBool());
        deviceWindow->close();
        QTRY_COMPARE_WITH_TIMEOUT(quits,1,patient(3000));QVERIFY(view->property("quitting").toBool());
        QVERIFY(!view->property("pending").toBool());
        view->setProperty("quitting",false);QVERIFY(QMetaObject::invokeMethod(view,"showMainWindow"));view->setProperty("detached",false);
        QTRY_VERIFY_WITH_TIMEOUT(window->isVisible(),patient(3000));
        // Losing the device window in device-only mode ends the app after the grace period.
        configure({{"pending_avd","Lifecycle"},{"profile_id","pixel-11"},{"detached",true},{"solo",true}});
        QTRY_VERIFY_WITH_TIMEOUT(deviceWindow->isVisible(),patient(3000));QTRY_VERIFY_WITH_TIMEOUT(!window->isVisible(),patient(3000));
        configure({{"pending_avd",""}});
        QTRY_VERIFY_WITH_TIMEOUT(!deviceWindow->isVisible(),patient(3000));
        QTest::qWait(1500);QCOMPARE(quits,1);
        QTRY_COMPARE_WITH_TIMEOUT(quits,2,patient(6000));
        view->setProperty("quitting",false);QVERIFY(QMetaObject::invokeMethod(view,"showMainWindow"));view->setProperty("detached",false);
        // A device-only app whose device never appears gets its main window back.
        view->setProperty("soloFallbackMs",300);
        window->setVisible(false);view->setProperty("solo",true);
        QVERIFY(!window->isVisible());
        QTRY_VERIFY_WITH_TIMEOUT(window->isVisible(),patient(3000));QVERIFY(!view->property("solo").toBool());
        view->setProperty("soloFallbackMs",120000);
        QTest::qWait(200);QCOMPARE(quits,2);quits=0;
        window->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(window->isActive(),patient(3000));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void android_model_creation_is_available_without_raw_tool_forms() {
        open("devices");workspace->setProperty("selectedPlatform","android");
        auto *button=item("createAndroidEmulator");QVERIFY(button);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,button->mapToScene(QPointF(button->width()/2,button->height()/2)).toPoint());
        auto *dialog=object("createAndroidDialog");QVERIFY(dialog);QTRY_VERIFY(dialog->property("visible").toBool());
        auto *picker=object("androidModelPicker");QVERIFY(picker);int android=0;for(const auto &p:backend->models())android+=p.toMap()["platform"]=="android";
        QCOMPARE(picker->property("count").toInt(),android);
        auto *name=item("androidAvdName");QVERIFY(name);QCOMPARE(name->property("text").toString(),QString("Tomacco-pixel-11"));
        capture("android-model-create");QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void android_performance_settings_require_stopped_valid_hardware() {
        open("devices");workspace->setProperty("selectedPlatform","android");
        auto *library=object("androidDeviceLibrary");QVERIFY(library);
        const auto previous=workspace->property("devices");
        workspace->setProperty("devices",QVariantList{QVariantMap{{"id","emulator-settings"},{"name","Performance emulator"},{"avd","Performance"},{"platform","android"},{"state","device"}}});
        QVariant avd=QVariantMap{{"name","Performance"},{"gpu","host"},{"memory_mb",4096},{"cores",4}};
        QVERIFY(QMetaObject::invokeMethod(library,"performance",Q_ARG(QVariant,avd)));
        auto *dialog=object("androidPerformanceDialog");auto *save=item("saveAndroidPerformance");
        auto *gpu=object("androidGpuPicker");auto *memory=object("androidMemoryField");
        QVERIFY(dialog);QVERIFY(save);QVERIFY(gpu);QVERIFY(memory);
        QTRY_VERIFY(dialog->property("visible").toBool());
        QCOMPARE(gpu->property("currentIndex").toInt(),1);
        QCOMPARE(memory->property("text").toString(),QString("4096"));
        QVERIFY(!save->isEnabled());QVERIFY(item("stopPerformanceAvd")->isVisible());
        workspace->setProperty("devices",QVariantList{});
        QTRY_VERIFY(save->isEnabled());
        memory->setProperty("text","0");QVERIFY(!save->isEnabled());
        memory->setProperty("text","8192");QVERIFY(save->isEnabled());
        capture("android-performance");
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        workspace->setProperty("devices",previous);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void devices_can_be_renamed_and_pinned() {
        open("devices");workspace->setProperty("selectedPlatform","android");
        auto *library=object("androidDeviceLibrary");QVERIFY(library);
        const auto labels=[this]{QFile file(directory.filePath("state/device-labels.json"));return file.open(QIODevice::ReadOnly)?QJsonDocument::fromJson(file.readAll()).object().toVariantMap():QVariantMap{};};
        const auto avds=[&](const QVariantMap &extra){QVariantMap avd{{"name","Label_Test"},{"profile_name","Pixel 11"},{"display",QVariantMap{{"width",1080},{"height",2400}}}};for(auto i=extra.begin();i!=extra.end();++i)avd[i.key()]=i.value();library->setProperty("avds",QVariantList{QVariantMap{{"name","Other"},{"display",QVariantMap{}}},avd});};
        avds({});
        // A pinned emulator comes first.
        auto *pin=item("pinAvd-Label_Test");QVERIFY(pin);
        QCOMPARE(accessibleName(pin),QString("Pin"));
        QVERIFY(QMetaObject::invokeMethod(pin,"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(labels().value("avd:Label_Test").toMap().value("pinned").toBool(),patient(10000));
        avds({{"pinned",true}});
        QCOMPARE(mapOf(library->property("shownAvds").toList().value(0)).value("name").toString(),QString("Label_Test"));
        QTRY_COMPARE(accessibleName(item("pinAvd-Label_Test")),QString("Unpin"));
        // A nickname is saved through device.label and shown in place of the model.
        QVERIFY(QMetaObject::invokeMethod(item("renameAvd-Label_Test"),"clicked"));
        auto *dialog=object("renameDeviceDialog");QVERIFY(dialog);QTRY_VERIFY(dialog->property("visible").toBool());
        auto *field=item("deviceNicknameField");auto *save=item("saveDeviceNickname");QVERIFY(field);QVERIFY(save);
        QVERIFY(!save->isEnabled());
        field->setProperty("text","  Test phone ");QVERIFY(save->isEnabled());
        capture("device-nickname");
        QVERIFY(QMetaObject::invokeMethod(save,"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(!dialog->property("visible").toBool(),patient(10000));
        QCOMPARE(labels().value("avd:Label_Test").toMap(),(QVariantMap{{"nickname","Test phone"},{"pinned",true}}));
        avds({{"pinned",true},{"nickname","Test phone"}});
        QTRY_COMPARE(item("avdTitle-Label_Test")->property("text").toString(),QString("Test phone"));
        // Removing the nickname and unpinning leave no label behind.
        QVERIFY(QMetaObject::invokeMethod(item("renameAvd-Label_Test"),"clicked"));
        QTRY_VERIFY(dialog->property("visible").toBool());
        QCOMPARE(field->property("text").toString(),QString("Test phone"));
        QVERIFY(QMetaObject::invokeMethod(item("removeDeviceNickname"),"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(!dialog->property("visible").toBool(),patient(10000));
        avds({{"pinned",true}});
        QVERIFY(QMetaObject::invokeMethod(item("pinAvd-Label_Test"),"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(!labels().contains("avd:Label_Test"),patient(10000));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void wireless_pairing_shows_a_qr_code_and_falls_back_to_the_pairing_code() {
        // A stand-in adb: the phone offers the code's service once the test says it scanned it.
        auto fake=useFakeAdb("case \"$1\" in\n"
             " mdns) echo 'List of discovered mdns services'; [ -f \"$d/scanned\" ] && printf '%s\\t_adb-tls-pairing._tcp.\\t192.168.1.20:41235\\n' \"$(cat \"$d/scanned\")\"; [ -f \"$d/paired\" ] && printf 'adb-R5CT-AbCd\\t_adb-tls-connect._tcp.\\t192.168.1.20:37099\\n';;\n"
             " pair) touch \"$d/paired\"; echo \"Successfully paired to $2 [guid=adb-R5CT-AbCd]\";;\n"
             " connect) echo \"connected to $2\";;\n"
             " devices) echo 'List of devices attached';;\nesac");
        auto cleanup=qScopeGuard([&]{QFile::remove(directory.filePath("scanned"));QFile::remove(directory.filePath("paired"));});
        open("devices");workspace->setProperty("selectedPlatform","android");
        auto *library=object("androidDeviceLibrary");QVERIFY(library);
        auto *button=item("pairWifiButton");QVERIFY(button);QVERIFY(button->isVisible());
        QVERIFY(QMetaObject::invokeMethod(button,"clicked"));
        auto *dialog=object("pairWifiDialog");QVERIFY(dialog);QTRY_VERIFY(dialog->property("visible").toBool());
        // The code comes from android.pair and is drawn from its modules.
        QTRY_VERIFY2_WITH_TIMEOUT(library->property("pairState").toString()=="waiting",qPrintable(library->property("pairMessage").toString()),patient(10000));
        const auto pairing=mapOf(library->property("pairing"));
        QVERIFY(pairing.value("qr_text").toString().startsWith("WIFI:T:ADB;S:tomacco-"));
        QCOMPARE(object("pairQrCanvas")->property("rows").toList().size(),pairing.value("qr").toMap().value("size").toInt());
        QVERIFY(item("pairQrCode")->isVisible());
        capture("pair-qr");
        {QFile scanned(directory.filePath("scanned"));QVERIFY(scanned.open(QIODevice::WriteOnly));scanned.write(pairing.value("service").toByteArray());}
        QTRY_COMPARE_WITH_TIMEOUT(library->property("pairState").toString(),QString("done"),patient(20000));
        QCOMPARE(library->property("pairMessage").toString(),QString("Paired and connected · 192.168.1.20:37099"));
        QVERIFY(item("pairNewCode")->isVisible());
        // The pairing code: address and six digits, then the phone's connection service.
        QFile::remove(directory.filePath("paired"));
        QVERIFY(QMetaObject::invokeMethod(item("pairModeCode"),"clicked"));
        QTRY_COMPARE(library->property("pairMode").toString(),QString("code"));
        auto *address=item("pairAddressField");auto *code=item("pairCodeField");auto *pair=item("pairWithCode");
        QVERIFY(address);QVERIFY(code);QVERIFY(pair);
        // The one pairing service adb found fills in the address.
        QTRY_COMPARE_WITH_TIMEOUT(address->property("text").toString(),QString("192.168.1.20:41235"),patient(10000));
        QVERIFY(!pair->isEnabled());
        code->setProperty("text","12345");QVERIFY(!pair->isEnabled());
        code->setProperty("text","123456");QVERIFY(pair->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(pair,"clicked"));
        QTRY_COMPARE_WITH_TIMEOUT(library->property("pairState").toString(),QString("done"),patient(20000));
        QCOMPARE(library->property("pairMessage").toString(),QString("Paired and connected · 192.168.1.20:37099"));
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QCOMPARE(library->property("pairState").toString(),QString());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void the_live_screen_is_recorded_until_stopped_and_the_video_shown() {
        // Recordings go to the videos folder the desktop names, here a temporary one.
        QTemporaryDir videos;QVERIFY(videos.isValid());
        {QFile dirs(directory.filePath("config/user-dirs.dirs"));QVERIFY(QDir().mkpath(directory.filePath("config")));QVERIFY(dirs.open(QIODevice::WriteOnly));dirs.write(("XDG_VIDEOS_DIR=\""+videos.path()+"\"\n").toUtf8());}
        auto dirs=qScopeGuard([&]{QFile::remove(directory.filePath("config/user-dirs.dirs"));});
        // screenrecord runs until pkill interrupts it; pull writes the video.
        auto fake=useFakeAdb("case \"$*\" in\n"
             " *screenrecord\\ --time-limit*) i=0; while [ ! -f \"$d/interrupted\" ] && [ $i -lt 600 ]; do sleep 0.05; i=$((i+1)); done;;\n"
             " *pkill*) touch \"$d/interrupted\";;\n"
             " *\\ pull\\ *) printf 'mp4 video' > \"$5\";;\n"
             " devices*) echo 'List of devices attached';;\nesac");
        auto cleanup=qScopeGuard([&]{QFile::remove(directory.filePath("interrupted"));});
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *display=qobject_cast<NativeView *>(object("nativeDisplay"));QVERIFY(display);
        view->setProperty("autoAttach",false);
        QLocalServer video,controlSocket;
        QVERIFY(video.listen(directory.filePath("record-video")));QVERIFY(controlSocket.listen(directory.filePath("record-input")));
        QImage image(QSize(400,800),QImage::Format_RGB32);image.fill(QColor("#2fa84f"));
        QByteArray bytes;{QBuffer output(&bytes);output.open(QIODevice::WriteOnly);image.save(&output,"JPEG");}
        QByteArray packet(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;
        connect(&video,&QLocalServer::newConnection,this,[&]{auto *socket=video.nextPendingConnection();socket->write(packet);connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        view->setProperty("targets",QVariantList{QVariantMap{{"id","emulator-5554"},{"name","Record test"},{"platform","android"},{"kind","emulator"}}});
        const QVariantMap session{{"session","record-session"},{"platform","android"},{"device","emulator-5554"},{"codec","mjpeg"},{"video_socket",video.fullServerName()},{"control_socket",controlSocket.fullServerName()}};
        QVERIFY(QMetaObject::invokeMethod(view,"attach",Q_ARG(QVariant,session)));
        QTRY_COMPARE_WITH_TIMEOUT(display->sourceSize(),QSize(400,800),patient(3000));
        auto *button=item("nativeRecord");QVERIFY(button);QTRY_VERIFY(button->isVisible());
        QCOMPARE(accessibleName(button),QString("Record the screen"));
        auto *recorder=object("nativeRecorder");QVERIFY(recorder);
        QSignalSpy revealed(backend.get(),&Backend::fileRevealed);
        QVERIFY(QMetaObject::invokeMethod(button,"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(recorder->property("recording").toBool(),patient(10000));
        QTRY_COMPARE(accessibleName(button),QString("Stop recording"));
        const auto path=recorder->property("path").toString();
        QVERIFY2(path.startsWith(videos.path()+"/Tomacco/Record-test-") && path.endsWith(".mp4"),qPrintable(path));
        capture("native-recording");
        QVERIFY(QMetaObject::invokeMethod(button,"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(!recorder->property("recording").toBool(),patient(20000));
        QVERIFY2(!recorder->property("failed").toBool(),qPrintable(recorder->property("notice").toString()));
        QCOMPARE(view->property("notice").toString(),QString("Recording saved · ")+path);
        QFile saved(path);QVERIFY(saved.open(QIODevice::ReadOnly));QCOMPARE(saved.readAll(),QByteArray("mp4 video"));
        QCOMPARE(revealed.size(),1);QCOMPARE(revealed.first().first().toString(),path);
        view->setProperty("live",QVariantMap{});display->setProperty("session",QVariantMap{});view->setProperty("targets",QVariantList{});
        // The virtual iPhone records through ios.virtual.record; with no phone running it says why.
        auto *guest=object("iosGuestRecorder");QVERIFY(guest);
        const QVariantMap phoneArgs{{"path",videos.filePath("phone.mp4")},{"seconds",600}};
        QVERIFY(QMetaObject::invokeMethod(guest,"start",Q_ARG(QVariant,QString("ios.virtual.record")),Q_ARG(QVariant,phoneArgs)));
        QTRY_VERIFY_WITH_TIMEOUT(guest->property("failed").toBool(),patient(10000));
        QVERIFY(!guest->property("recording").toBool());QVERIFY(!guest->property("pending").toBool());
        QVERIFY(!guest->property("notice").toString().isEmpty());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void all_running_devices_show_their_screens_side_by_side() {
        // Two running devices; adb answers each screencap with that device's picture.
        for(const auto &[serial,color]:{std::pair{"emulator-5554","#2fa84f"},std::pair{"R5CT","#295c83"}}) {
            QImage picture(108,240,QImage::Format_RGB32);picture.fill(QColor(color));QVERIFY(picture.save(directory.filePath(QString("screen-%1.png").arg(serial))));
        }
        auto fake=useFakeAdb("case \"$*\" in\n"
             " *exec-out\\ screencap*) cat \"$d/screen-$2.png\";;\n"
             " devices*) printf 'List of devices attached\\nemulator-5554 device model:Pixel\\nR5CT device model:Galaxy\\nOFF1 offline\\n';;\nesac");
        open("devices");workspace->setProperty("selectedPlatform","android");
        auto *library=object("androidDeviceLibrary");QVERIFY(library);
        const auto previous=workspace->property("devices");
        workspace->setProperty("devices",QVariantList{
            QVariantMap{{"id","emulator-5554"},{"name","Pixel"},{"platform","android"},{"state","device"},{"pinned",true}},
            QVariantMap{{"id","R5CT"},{"name","Galaxy"},{"platform","android"},{"state","device"}},
            QVariantMap{{"id","OFF1"},{"name","OFF1"},{"platform","android"},{"state","offline"}}});
        auto *toggle=item("deviceGridToggle");QVERIFY(toggle);QCOMPARE(accessibleName(toggle),QString("All screens"));
        QVERIFY(QMetaObject::invokeMethod(toggle,"clicked"));
        QTRY_VERIFY(library->property("gridMode").toBool());
        auto *grid=item("androidDeviceGrid");QVERIFY(grid);QTRY_VERIFY(grid->isVisible());
        // Only running devices, each with its own screen.
        QTRY_COMPARE(grid->property("devices").toList().size(),2);
        for(const auto &[serial,color]:{std::pair{"emulator-5554","#2fa84f"},std::pair{"R5CT","#295c83"}}) {
            QQuickItem *thumb=nullptr;
            QTRY_VERIFY_WITH_TIMEOUT((thumb=item(QString("deviceThumb-%1").arg(serial).toLatin1().constData())) && thumb->property("status").toInt()==1,patient(15000));
            const auto shot=mapOf(mapOf(grid->property("shots")).value(serial));
            QCOMPARE(shot.value("width").toInt(),108);
            QCOMPARE(QImage(shot.value("path").toString()).pixelColor(5,5),QColor(color));
        }
        QVERIFY(!item("deviceTile-OFF1"));
        // A new picture at the same path loads again.
        const auto first=mapOf(mapOf(grid->property("shots")).value("R5CT")).value("stamp").toDouble();
        QTRY_VERIFY_WITH_TIMEOUT(mapOf(mapOf(grid->property("shots")).value("R5CT")).value("stamp").toDouble()>first,patient(10000));
        capture("device-grid");
        // A click opens that device live.
        QSignalSpy opened(workspace,SIGNAL(runtimeRequested(QString,QString)));
        QVERIFY(QMetaObject::invokeMethod(item("deviceTile-R5CT"),"clicked"));
        QCOMPARE(opened.size(),1);QCOMPARE(opened.first().value(1).toString(),QString("R5CT"));
        QCOMPARE(workspace->property("selectedDevice").toString(),QString("R5CT"));
        library->setProperty("gridMode",false);workspace->setProperty("devices",previous);
        open("devices");
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void the_live_log_follows_one_app_from_a_level_up() {
        // logcat names its arguments, then prints an error and an info line and keeps running.
        auto fake=useFakeAdb("case \"$*\" in\n"
             " *logcat*) echo \"ARGS $*\"; echo '10-31 12:00:00.000  100  100 E Fake: fake error line'; echo '10-31 12:00:00.001  100  100 I Fake: fake info line'; while true; do sleep 0.2; done;;\n"
             " *getprop\\ ro.build.version.sdk*) echo 36;;\n"
             " *list\\ packages\\ -U*) echo 'package:dev.test.app uid:10200';;\n"
             " devices*) printf 'List of devices attached\\nemulator-5554 device model:Pixel\\n';;\nesac");
        open("devices");workspace->setProperty("selectedPlatform","android");
        const auto previous=workspace->property("devices");
        workspace->setProperty("devices",QVariantList{QVariantMap{{"id","emulator-5554"},{"name","Pixel"},{"platform","android"},{"state","device"}}});
        workspace->setProperty("selectedDevice","emulator-5554");
        auto *toggle=item("deviceLogToggle");QVERIFY(toggle);QCOMPARE(accessibleName(toggle),QString("Live log"));
        QVERIFY(QMetaObject::invokeMethod(toggle,"clicked"));
        auto *pane=object("deviceLogPane");QVERIFY(pane);QTRY_VERIFY(qobject_cast<QQuickItem *>(pane)->isVisible());
        const auto text=[&]{return pane->property("text").toString();};
        QTRY_VERIFY2_WITH_TIMEOUT(text().contains("fake info line"),qPrintable(text()+pane->property("error").toString()),patient(15000));
        QVERIFY(text().contains("ARGS -s emulator-5554 logcat -v threadtime\n"));
        const auto firstJob=pane->property("job").toString();QVERIFY(!firstJob.isEmpty());
        // From warnings up: logcat filters, and a new capture starts.
        QVERIFY(QMetaObject::invokeMethod(item("deviceLogLevel-W"),"clicked"));
        QTRY_VERIFY_WITH_TIMEOUT(text().contains("logcat -v threadtime *:W\n"),patient(15000));
        // One app, by its user ID.
        auto *app=item("deviceLogApp");QVERIFY(app);
        app->setProperty("text","dev.test.app");QVERIFY(QMetaObject::invokeMethod(app,"editingFinished"));
        QTRY_VERIFY_WITH_TIMEOUT(text().contains("logcat -v threadtime --uid=10200 *:W\n"),patient(15000));
        // Searching keeps the lines with the text, coloured by level.
        item("deviceLogSearch")->setProperty("text","error line");
        QTRY_COMPARE(pane->property("lines").toList().size(),1);
        capture("device-log");
        item("deviceLogSearch")->setProperty("text","");
        // An app that is not installed is said, not followed.
        app->setProperty("text","dev.missing.app");QVERIFY(QMetaObject::invokeMethod(app,"editingFinished"));
        QTRY_COMPARE_WITH_TIMEOUT(pane->property("error").toString(),QString("dev.missing.app is not installed"),patient(10000));
        QVERIFY(pane->property("job").toString().isEmpty());
        // Closing the pane ends its capture.
        app->setProperty("text","");QVERIFY(QMetaObject::invokeMethod(app,"editingFinished"));
        QTRY_VERIFY_WITH_TIMEOUT(!pane->property("job").toString().isEmpty(),patient(10000));
        const auto lastJob=pane->property("job").toString();
        QVERIFY(QMetaObject::invokeMethod(toggle,"clicked"));
        QTRY_VERIFY(!qobject_cast<QQuickItem *>(pane)->isVisible());
        QVERIFY(pane->property("job").toString().isEmpty());
        for(const auto &job:{firstJob,lastJob}) {
            QString state;
            QTRY_VERIFY_WITH_TIMEOUT(([&]{
                QSignalSpy replies(control.get(),&Control::apiFinished);
                const int id=control->request("job.get",{{"job_id",job}});
                if(!replies.wait(patient(5000)))return false;
                for(const auto &reply:replies)if(reply.value(0).toInt()==id)state=reply.value(2).toMap().value("result").toMap().value("state").toString();
                return state=="cancelled";
            })(),patient(20000));
        }
        workspace->setProperty("devices",previous);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void iphone_setup_and_category_selection() {
        open("devices");
        auto *apple=item("devices-apple");QVERIFY(apple);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,apple->mapToScene(QPointF(apple->width()/2,apple->height()/2)).toPoint());
        QCOMPARE(workspace->property("selectedPlatform").toString(),QString("ios"));
        capture("apple-devices");
        open("runtime");
        QVariant config=QVariantMap{{"platform","ios"},{"apple_target","device"},{"setup",true}};
        QVERIFY(QMetaObject::invokeMethod(window,"configureNative",Q_ARG(QVariant,config)));
        auto *setup=object("iphoneSetup");QVERIFY(setup);
        QTRY_VERIFY(setup->property("visible").toBool());
        QTest::qWait(1200);capture("iphone-setup");
        auto *closeButton=item("iphoneSetup-button-1");QVERIFY(closeButton);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,closeButton->mapToScene(QPointF(closeButton->width()/2,closeButton->height()/2)).toPoint());
        QTRY_VERIFY(!setup->property("visible").toBool());capture("iphone-runtime");
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void ios_research_controls_and_capability_remain_honest() {
        open("ios-research");
        QCOMPARE(workspace->property("selectedPlatform").toString(),QString("ios"));
        auto *view=object("iosResearch");QVERIFY(view);
        auto *dialog=object("iosResearchDialog");QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        auto *native=object("nativeWorkspace");QVERIFY(native);
        const auto previousPlatform=native->property("platform"), previousTarget=native->property("target");
        const auto selectedDevice=workspace->property("selectedDevice");
        native->setProperty("platform","android");native->setProperty("target","background-refresh-device");
        QCOMPARE(workspace->property("selectedPlatform").toString(),QString("ios"));
        QCOMPARE(workspace->property("selectedDevice"),selectedDevice);
        native->setProperty("platform",previousPlatform);native->setProperty("target",previousTarget);
        auto *start=item("iosResearchStart");auto *stop=item("iosResearchStop");
        QVERIFY(start);QVERIFY(stop);QVERIFY(!start->isEnabled());QVERIFY(!stop->isEnabled());
        // The virtual iPhone row follows the shared state: a start can be
        // stopped, a stop cannot be repeated, and why a stop left the last run
        // unsaved stays readable. (The row sits behind this dialog, so it is
        // judged by its own state, not by what is on screen.)
        auto *phoneStart=qobject_cast<QQuickItem *>(object("virtualPhoneStart"));auto *phoneStop=qobject_cast<QQuickItem *>(object("virtualPhoneStop"));
        auto *phoneState=object("virtualPhoneState");auto *phoneError=qobject_cast<QQuickItem *>(object("virtualPhoneError"));
        QVERIFY(phoneStart);QVERIFY(phoneStop);QVERIFY(phoneState);QVERIFY(phoneError);
        const auto phone=[&](const QString &phase,bool running,const QString &busy,const QString &error={}){
            QVariantMap value{{"configured",true},{"run",5},{"phase",phase},{"guest_running",running},{"error",error.isEmpty()?QVariant():QVariant(error)}};
            if(!busy.isEmpty())value["busy"]=QVariantMap{{"action",busy}};
            view->setProperty("virtualPhone",value);
        };
        phone("booting",false,"ios.virtual.start");
        QVERIFY(phoneStop->property("visible").toBool());QVERIFY(phoneStop->isEnabled());QCOMPARE(phoneStop->property("text").toString(),QString("Cancel"));
        QVERIFY(!phoneStart->isEnabled());QVERIFY(phoneState->property("text").toString().contains("iOS is starting"));
        phone("ready",true,{});
        QVERIFY(phoneStop->isEnabled());QCOMPARE(phoneStop->property("text").toString(),QString("Stop"));QVERIFY(!phoneStart->property("visible").toBool());
        phone("stopping",true,"ios.virtual.stop");
        QVERIFY(phoneStop->property("visible").toBool());QVERIFY(!phoneStop->isEnabled());
        QVERIFY(phoneState->property("text").toString().contains("stopping"));
        phone("unverified",false,{},"The guest did not answer and was ended without saving; the next start uses the last verified run");
        QVERIFY(!phoneStop->property("visible").toBool());QVERIFY(phoneStart->isEnabled());
        QVERIFY(phoneState->property("text").toString().contains("not verified"));
        QVERIFY(phoneError->property("visible").toBool());QVERIFY(phoneError->property("text").toString().contains("without saving"));
        // A stop that was refused because the guest is working leaves it running, with the reason shown.
        phone("ready",true,{},"Stop refused: A native framework request is still in progress");
        QVERIFY(phoneStop->isEnabled());QVERIFY(phoneError->property("visible").toBool());
        // The device model is a choice of its own: the profile's phone with a
        // Home button, Apple's variant, or a model derived from it. A change
        // made while the phone runs is announced as taking effect later.
        auto *modelPicker=object("virtualPhoneModel");QVERIFY(modelPicker);
        const QVariantList choices{QVariantMap{{"id","16"},{"name","iPhone 16"},{"source","apple"},{"panel","1179x2556@60:65x141"}},
                                   QVariantMap{{"id","18pro"},{"name","iPhone 18 Pro"},{"source","derived"},{"panel","1206x2622@60:66x143"}}};
        const auto phoneModel=[&](const QString &next,const QString &current,bool running){
            view->setProperty("virtualPhone",QVariantMap{{"configured",true},{"run",5},{"phase",running?"ready":"stopped"},{"guest_running",running},
                {"models",choices},{"model",next},{"running_model",current}});
        };
        phoneModel("","",false);
        QCOMPARE(modelPicker->property("count").toInt(),3);QCOMPARE(modelPicker->property("currentIndex").toInt(),0);
        QVERIFY(modelPicker->property("currentText").toString().contains("Home button"));
        phoneModel("16","16",true);
        QCOMPARE(modelPicker->property("currentIndex").toInt(),1);
        QVERIFY(modelPicker->property("currentText").toString().contains("Apple variant"));
        QVERIFY(!view->property("virtualModelPending").toBool());
        phoneModel("18pro","16",true);
        QCOMPARE(modelPicker->property("currentIndex").toInt(),2);
        QVERIFY(modelPicker->property("currentText").toString().contains("derived"));
        QVERIFY(view->property("virtualModelPending").toBool());
        // A model this build does not know falls back to the first entry instead of an empty box.
        phoneModel("unknown-model","",false);
        QCOMPARE(modelPicker->property("currentIndex").toInt(),0);
        // The Apple page has to show everything at once also at the smallest window: the virtual iPhone's
        // card with its phone row, model row, job state and last error, and the action buttons below the list.
        auto *actions=item("deviceActions");QVERIFY(actions);
        auto *card=qobject_cast<QQuickItem *>(view);QVERIFY(card);
        const QVariantMap everything{{"configured",true},{"run",5},{"phase","unverified"},{"guest_running",false},{"models",choices},{"model","16"},{"running_model",""},
            {"error","The guest did not answer and was ended without saving; the next start uses the last verified run"}};
        view->setProperty("jobId","fixture-layout");
        for(const auto &size:{QSize(960,680),QSize(1380,970)}) {
            // A status answer arriving meanwhile would replace the phone state and shrink the card; try again then.
            for(int attempt=0;;++attempt) {
                view->setProperty("virtualPhone",everything);
                window->resize(size);QTest::qWait(150);
                if(mapOf(view->property("virtualPhone")).value("configured").toBool())break;
                QVERIFY2(attempt<5,"the virtual iPhone state keeps being replaced");
            }
            // The layout does not squeeze the card's rows into each other ...
            QVERIFY2(card->height()>=card->implicitHeight()-1,qPrintable(QString("card %1 of %2 at %3x%4").arg(card->height()).arg(card->implicitHeight()).arg(size.width()).arg(size.height())));
            // ... and the action buttons can be reached: they fit, or the page scrolls to them.
            QQuickItem *page=nullptr;
            for(auto *parent=actions->parentItem();parent && !page;parent=parent->parentItem())if(parent->inherits("QQuickFlickable"))page=parent;
            if(page)page->setProperty("contentY",qMax(0.0,page->property("contentHeight").toReal()-page->height()));
            QTest::qWait(50);
            const auto viewport=page?page->mapRectToScene(page->boundingRect()):QRectF(0,0,window->width(),window->height());
            int shown=0;
            for(auto *child:actions->childItems()) {
                if(!child->isVisible())continue;
                ++shown;
                const auto bounds=child->mapRectToScene(child->boundingRect());
                QVERIFY2(bounds.top()>=viewport.top()-1 && bounds.bottom()<=viewport.bottom()+1 && bounds.bottom()<=window->height(),
                    qPrintable(QString("%1 spans %2..%3 in a %4x%5 window").arg(child->property("text").toString()).arg(bounds.top()).arg(bounds.bottom()).arg(size.width()).arg(size.height())));
            }
            QVERIFY(shown>=5);
            capture("ios-devices-end-"+QString::number(size.width()));
            if(page)page->setProperty("contentY",0);
        }
        view->setProperty("jobId","");
        view->setProperty("virtualPhone",QVariantMap{{"configured",false}});
        QVERIFY(!phoneError->property("visible").toBool());
        view->setProperty("enginePath","/test-only/engine");
        view->setProperty("bundlePath","/test-only/bundle");QVERIFY(start->isEnabled());
        view->setProperty("starting",true);QVERIFY(!start->isEnabled());QVERIFY(!stop->isEnabled());
        view->setProperty("starting",false);
        const QVariantMap platformReport{{"snapshots",QVariantList{QVariantMap{},QVariantMap{}}},
            {"missing_services",QStringList{"IOMobileFramebuffer","AppleSEPManager"}}};
        const QVariantMap report{{"state","research_observed"},{"guest_observations_verified",true},
            {"graphical_apps_verified",false},{"platform",platformReport}};
        QVariant observed=QVariantMap{{"job_state","succeeded"},{"report",report}};
        QVERIFY(QMetaObject::invokeMethod(view,"applyReport",Q_ARG(QVariant,observed)));
        QCOMPARE(view->property("stateLabel").toString(),QString("Diagnostics complete"));
        QVERIFY(start->isEnabled());QVERIFY(!stop->isEnabled());
        QVERIFY(object("iosResearchCapability")->property("text").toString().contains("have not been shown to work yet"));
        for(const auto &size:{QSize(960,680),QSize(1380,970)}) {
            window->resize(size);QTest::qWait(150);
            const auto bounds=start->mapRectToScene(start->boundingRect());
            QVERIFY(bounds.left()>=0 && bounds.right()<=window->width());
            capture("ios-research-"+QString::number(size.width()));
        }
        QVariant cancelled=QVariantMap{{"job_state","cancelled"},{"report",QVariantMap{{"state","observing"}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"applyReport",Q_ARG(QVariant,cancelled)));
        QCOMPARE(view->property("stateLabel").toString(),QString("Run ended"));
        QVERIFY(start->isEnabled());
        view->setProperty("mode","pmp");
        QVERIFY(!start->isEnabled());
        view->setProperty("pmpBundlePath","/test-only/pmp");
        QVERIFY(start->isEnabled());
        QCOMPARE(view->property("bundlePath").toString(),QString("/test-only/bundle"));
        const QVariantMap arguments{{"engine","/agent/engine"},{"firmware","/agent/pmp"},{"seconds",12},{"controller","asc-v6"},{"stop_at","0x102ade0"},{"capture_ram",true},{"cpu_midr","0x611f0521"},{"cpu_affinity","0x10000"}};
        QVariant agentJob=QVariantMap{{"id","fixture-pmp"},{"state","failed"},{"created",1234},
            {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.pmp"},{"arguments",arguments}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,agentJob)));
        QCOMPARE(view->property("pmpBundlePath").toString(),QString("/agent/pmp"));
        QCOMPARE(view->property("pmpSeconds").toInt(),12);
        QCOMPARE(view->property("stopAddress").toString(),QString("0x102ade0"));
        QVERIFY(view->property("captureRam").toBool());
        QCOMPARE(view->property("cpuMidr").toString(),QString("0x611f0521"));
        QCOMPARE(view->property("cpuAffinity").toString(),QString("0x10000"));
        view->setProperty("showCpuConfig",true);
        QCOMPARE(object("iosResearchCpuMidr")->property("text").toString(),QString("0x611f0521"));
        const QVariantMap controller{{"reads",42},{"writes",17},{"rejected_accesses",1},
            {"queues",QVariantList{QVariantMap{{"direction","A2I"},{"count",2},{"enabled",true},{"staged",false}}}},
            {"timers",QVariantList{QVariantMap{{"source","ARM physical timer"},{"state",2},{"input_asserted",false}}}}};
        const QVariantMap fault{{"pc","0x1000000"},{"physical","0x300e00044"},{"access","write"}};
        QVariant pmpReport=QVariantMap{{"job_state","failed"},{"report",QVariantMap{
            {"cpu_policy",QVariantMap{{"midr","0x611f0521"},{"affinity","0x10000"}}},{"cpu_identity_verified",true},
            {"observations",QVariantList{QVariantMap{{"controller_state",controller}}}},
            {"sysreg_faults",QVariantMap{{"events",QVariantList{QVariantMap{{"pc","0x102adac"},{"register","S3_7_c15_c4_6"},{"access","read"}}}}}},
            {"bus_faults",QVariantMap{{"events",QVariantList{fault}}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"applyReport",Q_ARG(QVariant,pmpReport)));
        QVERIFY(object("iosResearchControllerState")->property("text").toString().contains("42 reads"));
        QVERIFY(object("iosResearchFirstFault")->property("text").toString().contains("write"));
        QVERIFY(object("iosResearchSysregFault")->property("text").toString().contains("S3_7_c15_c4_6"));
        QVERIFY(object("iosResearchCapability")->property("text").toString().contains("not verified"));
        QVERIFY(object("iosResearchCpuIdentity")->property("text").toString().contains("MIDR 0x611f0521 · core ID 0x10000 · configuration verified"));
        capture("ios-research-pmp");
        view->setProperty("showCpuConfig",false);
        const QVariantList registers{QVariantMap{{"name","PC"},{"value","0x000000000102ade0"}},
            QVariantMap{{"name","X00"},{"value","0xffffffffcc810080"}}};
        QVariant breakpointReport=QVariantMap{{"job_state","succeeded"},{"report",QVariantMap{
            {"breakpoint_hit",true},{"breakpoint_stop",QVariantMap{{"address","0x102ade0"}}},
            {"memory_capture",QVariantMap{{"physical_base","0x1000000"}}},
            {"observations",QVariantList{QVariantMap{{"cpu_registers",registers},{"controller_state",controller}}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"applyReport",Q_ARG(QVariant,breakpointReport)));
        QCOMPARE(view->property("stateLabel").toString(),QString("Breakpoint reached"));
        QVERIFY(object("iosResearchBreakpointResult")->property("text").toString().contains("0x102ade0"));
        QVERIFY(item("iosResearchMemoryRead")->isEnabled());
        view->setProperty("memoryResult",QVariantMap{{"address","0x1000000"},{"bytes",4},{"data","0041ff42"},{"ascii",".A.B"}});
        QVERIFY(object("iosResearchMemoryData")->property("text").toString().contains("01000000  00 41 ff 42"));
        QVERIFY(object("iosResearchMemoryData")->property("text").toString().endsWith(".A.B"));
        view->setProperty("showRegisters",true);
        QTest::qWait(100);
        auto *scroll=qobject_cast<QQuickItem *>(object("iosResearchScroll"));QVERIFY(scroll);
        auto *flick=scroll->property("contentItem").value<QObject *>();QVERIFY(flick);
        flick->setProperty("contentY",flick->property("contentHeight").toReal()-scroll->height());
        capture("ios-research-memory");
        view->setProperty("jobId","");
        QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,agentJob)));
        QVERIFY(object("iosResearchMemoryData")->property("text").toString().isEmpty());
        QVariant virtualJob=QVariantMap{{"id","fixture-virtual"},{"state","succeeded"},{"created",1235},
            {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.paravirtual"},
            {"arguments",QVariantMap{{"engine","/agent/virtual-engine"},{"interactive",true},{"seconds",600}}}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,virtualJob)));
        QCOMPARE(view->property("mode").toString(),QString("paravirtual"));
        QVERIFY(item("iosGuestView")->isVisible());
        QVERIFY(!item("iosResearchStart")->isVisible());
        QVERIFY(!item("iosGuestHome")->isEnabled());
        QVERIFY(!view->property("canStart").toBool());
        // Changing the selected guest must discard the prior guest's pixels.
        auto *guestView=object("iosGuestView");QVERIFY(guestView);
        guestView->setProperty("frameSource","data:image/png;base64,old");
        guestView->setProperty("jobId","fixture-next-virtual");
        QVERIFY(guestView->property("frameSource").toString().isEmpty());
        // A tap made while another request is in flight is queued, not lost,
        // and goes out as soon as that request has finished.
        guestView->setProperty("running",true);
        QVERIFY(QMetaObject::invokeMethod(guestView,"send",Q_ARG(QVariant,QString("status")),Q_ARG(QVariant,QVariant())));
        QVERIFY(guestView->property("pending").toInt()!=0);
        const QVariant tap=QVariantMap{{"x",.5},{"y",.5},{"duration_ms",60}};
        QVERIFY(QMetaObject::invokeMethod(guestView,"send",Q_ARG(QVariant,QString("click")),Q_ARG(QVariant,tap)));
        QVERIFY(QMetaObject::invokeMethod(guestView,"send",Q_ARG(QVariant,QString("home")),Q_ARG(QVariant,QVariant())));
        QCOMPARE(guestView->property("inputQueue").toList().size(),2);
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("inputQueue").toList().size(),0,patient(8000));
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("pending").toInt(),0,patient(8000));
        guestView->setProperty("running",false);
        QCOMPARE(guestView->property("inputQueue").toList().size(),0);
        // A guest described as a current iPhone gets that model's frame in its
        // own window: the picture sits in the frame's screen, cut to its
        // corners, and the island belongs to the frame. Without a model it is
        // the phone with the Home button.
        auto *guestWindow=qobject_cast<QQuickWindow *>(object("iosGuestWindow"));QVERIFY(guestWindow);
        auto *modernFrame=qobject_cast<QQuickItem *>(object("iosGuestModernFrame"));QVERIFY(modernFrame);
        auto *picture=qobject_cast<QQuickItem *>(object("iosGuestFrame"));QVERIFY(picture);
        auto *homeButton=qobject_cast<QQuickItem *>(object("iosGuestHomeButton"));QVERIFY(homeButton);
        QVariantMap iphone;for(const auto &p:backend->models())if(p.toMap()["id"]=="18pro")iphone=p.toMap();
        QVERIFY(!iphone.isEmpty());
        guestView->setProperty("running",true);guestView->setProperty("frameWidth",1206);guestView->setProperty("frameHeight",2622);
        guestView->setProperty("detached",true);
        QTRY_VERIFY_WITH_TIMEOUT(guestWindow->isVisible(),patient(3000));
        QVERIFY(!guestView->property("modern").toBool());QVERIFY(!modernFrame->isVisible());QVERIFY(homeButton->isVisible());
        QVERIFY(picture->parentItem()!=modernFrame->property("screen").value<QQuickItem *>());
        const auto classicWidth=guestWindow->property("deviceWidth").toReal();
        // The picture takes the wheel for the guest, but Ctrl+wheel belongs to the window around it, as over
        // the bezel: it zooms the device window and never becomes input for the guest.
        auto *contact=qobject_cast<QQuickItem *>(object("iosGuestContact"));QVERIFY(contact);
        QImage still(12,26,QImage::Format_RGB32);still.fill(QColor("#295c83"));
        QByteArray png;QBuffer pngOut(&png);pngOut.open(QIODevice::WriteOnly);still.save(&pngOut,"PNG");
        guestView->setProperty("guest",QVariantMap{{"state",QVariantMap{{"running",true}}}});
        guestView->setProperty("frameSource","data:image/png;base64,"+QString::fromLatin1(png.toBase64()));
        QTRY_VERIFY_WITH_TIMEOUT(contact->isEnabled() && contact->width()>0 && contact->height()>0,patient(3000));
        const auto wheelOverPicture=[&](Qt::KeyboardModifiers modifiers,int delta){
            const auto at=contact->mapToScene(QPointF(contact->width()/2,contact->height()/2));
            QWheelEvent event(at,QPointF(guestWindow->mapToGlobal(at.toPoint())),{},QPoint(0,delta),Qt::NoButton,modifiers,Qt::NoScrollPhase,false);
            QCoreApplication::sendEvent(guestWindow,&event);
        };
        const auto ctrlWheelZoomsTheWindow=[&]{
            QVERIFY(QMetaObject::invokeMethod(guestWindow,"setZoom",Q_ARG(QVariant,1)));
            const auto before=guestWindow->property("zoom").toReal();
            guestView->setProperty("wheelX",0);guestView->setProperty("wheelY",0);
            wheelOverPicture(Qt::ControlModifier,-120);
            QTRY_VERIFY_WITH_TIMEOUT(guestWindow->property("zoom").toReal()<before,patient(3000));
            QCOMPARE(guestView->property("wheelX").toReal(),0.0);QCOMPARE(guestView->property("wheelY").toReal(),0.0);
            // A plain wheel is still the guest's: it is recorded for the next scroll gesture (too small to be sent).
            wheelOverPicture(Qt::NoModifier,1);
            QCOMPARE(guestView->property("wheelY").toReal(),1.0);
            QTest::qWait(250);
            QVERIFY(guestView->property("inputQueue").toList().isEmpty());
        };
        ctrlWheelZoomsTheWindow();
        guestView->setProperty("deviceProfile",iphone);
        QVERIFY(guestView->property("modern").toBool());
        QTRY_VERIFY_WITH_TIMEOUT(modernFrame->isVisible(),patient(3000));QVERIFY(!homeButton->isVisible());
        QCOMPARE(picture->parentItem(),modernFrame->property("screen").value<QQuickItem *>());
        QCOMPARE(picture->width(),picture->parentItem()->width());QCOMPARE(picture->height(),picture->parentItem()->height());
        QCOMPARE(guestWindow->property("deviceWidth").toReal(),modernFrame->width());
        QCOMPARE(guestWindow->property("deviceHeight").toReal(),modernFrame->height());
        // 402 points wide plus a thin bezel: narrower than the classic body's margins made it.
        QVERIFY(modernFrame->width()>402 && modernFrame->width()<classicWidth+40);
        QVERIFY(qAbs(picture->width()/picture->height()-1206.0/2622)<.002);
        QCOMPARE(modernFrame->property("cutout").toString(),iphone["cutout"].toString());
        QTRY_VERIFY_WITH_TIMEOUT(contact->isEnabled() && contact->width()>0 && contact->height()>0,patient(3000));
        ctrlWheelZoomsTheWindow();
        // Home on a device without a Home button: the Home usage, then the swipe
        // up from the bottom edge that also unlocks. Both wait their turn.
        guestView->setProperty("inputQueue",QVariantList{});
        // Wait out an in-flight frame request: with one, both inputs queue.
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("pending").toInt(),0,patient(8000));
        QVERIFY(QMetaObject::invokeMethod(guestView,"home"));
        const auto queued=guestView->property("inputQueue").toList();
        QVERIFY(guestView->property("pending").toInt()!=0);
        QCOMPARE(queued.size(),1);
        const auto swipe=queued[0].toMap();
        QCOMPARE(swipe["action"].toString(),QString("drag"));
        const auto gesture=swipe["args"].toMap();
        QVERIFY(gesture["y"].toReal()>.97 && gesture["to_y"].toReal()<.4 && gesture["duration_ms"].toInt()>=1000);
        guestView->setProperty("inputQueue",QVariantList{});
        guestView->setProperty("detached",false);guestView->setProperty("deviceProfile",QVariantMap{});
        QTRY_VERIFY_WITH_TIMEOUT(!guestWindow->isVisible(),patient(3000));
        QVERIFY(picture->parentItem()!=modernFrame->property("screen").value<QQuickItem *>());
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("pending").toInt(),0,patient(8000));
        guestView->setProperty("running",false);guestView->setProperty("frameWidth",0);guestView->setProperty("frameHeight",0);
        // The guest window had the keyboard; give it back to the main window.
        window->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(window->isActive(),patient(3000));
        view->setProperty("jobId","");view->setProperty("mode","ios");
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void layout_lab_page_fills_the_opening_in_every_state() {
        // Every model, every state: the page is exactly the opening, cut to its corners. The Duo's own
        // list of four radii once came out of the outline as square corners (not a JS array inside
        // the library, so its corners were NaN), and the page stood over the body's round ones.
        auto *device=object("phoneDevice");QVERIFY(device);
        auto *screen=qobject_cast<QQuickItem *>(object("deviceScreen"));QVERIFY(screen);
        auto *browser=qobject_cast<QQuickItem *>(object("previewBrowser"));QVERIFY(browser);
        const auto models=backend->models();
        for(int index=0;index<models.size();++index) {
            const auto model=models[index].toMap();
            QVERIFY(QMetaObject::invokeMethod(window,"selectDevice",Q_ARG(QVariant,index)));
            for(const bool open:{false,true})for(const bool landscape:{false,true}) {
                if(open && !model.contains("openWidth"))continue;
                QVERIFY(QMetaObject::invokeMethod(window,"selectDuoState",Q_ARG(QVariant,open),Q_ARG(QVariant,landscape)));
                const auto state=model["id"].toString()+(open?" open":" folded")+(landscape?" landscape":" portrait");
                const qreal longer=open?qMax(model["openWidth"].toReal(),model["openHeight"].toReal()):qMax(model["width"].toReal(),model["height"].toReal());
                const qreal shorter=open?qMin(model["openWidth"].toReal(),model["openHeight"].toReal()):qMin(model["width"].toReal(),model["height"].toReal());
                const qreal w=landscape?longer:shorter,h=landscape?shorter:longer;
                QCOMPARE(screen->width(),w);QCOMPARE(screen->height(),h);
                // The page covers the opening from its corner, whatever it renders at.
                QCOMPARE(browser->x(),0.0);QCOMPARE(browser->y(),0.0);
                QVERIFY2(qAbs(browser->width()*browser->scale()-w)<.01 && qAbs(browser->height()*browser->scale()-h)<.01,qPrintable(state));
                QCOMPARE(device->property("width").toReal(),w+device->property("bezel").toReal()*2);
                // Each corner of the mask is cut as far as its radius says.
                const auto path=device->property("screenPath").toString();
                QVERIFY2(!path.isEmpty() && !path.contains("NaN"),qPrintable(state));
                const auto radii=device->property("cornerRadii").toList();QCOMPARE(radii.size(),4);
                const QStringList corners{QString("L 0 0 "),QString("L %1 0 ").arg(w),QString("L %1 %2 ").arg(w).arg(h),QString("L 0 %1 ").arg(h)};
                for(int corner=0;corner<4;++corner)if(radii[corner].toReal()>0)QVERIFY2(!(path+" ").contains(corners[corner]),qPrintable(state+" "+path));
            }
        }
        QVERIFY(QMetaObject::invokeMethod(window,"selectDuoState",Q_ARG(QVariant,false),Q_ARG(QVariant,false)));
        QVERIFY(QMetaObject::invokeMethod(window,"selectDevice",Q_ARG(QVariant,0)));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void layout_lab_emulates_media_and_network_per_session() {
        PageServer web("<!doctype html><title>media</title><p>media");QVERIFY(web.server.isListening());
        const bool wasNative=window->property("nativeMode").toBool();window->setProperty("nativeMode",false);
        const auto first=previewCall("state");auto state=first.value("result").toMap();
        QVERIFY2(first.contains("result"),qPrintable(QJsonDocument::fromVariant(first).toJson()));
        QCOMPARE(state.value("color_scheme").toString(),QString("system"));QCOMPARE(state.value("reduced_motion").toString(),QString("system"));
        QCOMPARE(state.value("network").toMap().value("preset").toString(),QString("none"));
        QVERIFY(!state.value("emulation").toMap().value("attached").toBool());
        QVERIFY(openPage(web.url()));
        const QString media="[matchMedia('(prefers-color-scheme: dark)').matches,matchMedia('(prefers-color-scheme: light)').matches,matchMedia('(prefers-reduced-motion: reduce)').matches]";
        auto reply=previewCall("configure",{{"color_scheme","dark"},{"reduced_motion","reduce"}});
        QVERIFY2(!reply.contains("error"),qPrintable(reply.value("error").toMap().value("message").toString()));
        QCOMPARE(reply.value("result").toMap().value("color_scheme").toString(),QString("dark"));
        QVERIFY(reply.value("result").toMap().value("emulation").toMap().value("attached").toBool());
        QCOMPARE(evaluate(media).toList(),(QVariantList{true,false,true}));
        QVERIFY(!previewCall("configure",{{"color_scheme","light"}}).contains("error"));
        QCOMPARE(evaluate(media).toList(),(QVariantList{false,true,true}));
        // The page keeps it across a reload.
        QVERIFY(openPage(web.url()+"?again"));
        QCOMPARE(evaluate(media).toList(),(QVariantList{false,true,true}));
        // Throttled: a request waits at least the added latency.
        const QString timed="(()=>{const t=performance.now();const x=new XMLHttpRequest();x.open('GET','/data?'+Math.random(),false);x.send();return [x.status,Math.round(performance.now()-t)]})()";
        reply=previewCall("configure",{{"network",QVariantMap{{"latency_ms",400}}}});
        QVERIFY2(!reply.contains("error"),qPrintable(reply.value("error").toMap().value("message").toString()));
        QCOMPARE(reply.value("result").toMap().value("network").toMap().value("preset").toString(),QString("custom"));
        auto measured=evaluate(timed).toList();QCOMPARE(measured.value(0).toInt(),200);QVERIFY2(measured.value(1).toInt()>=380,qPrintable(measured.value(1).toString()));
        QVERIFY(!previewCall("configure",{{"network",QVariantMap{{"preset","offline"}}}}).contains("error"));
        QCOMPARE(evaluate("navigator.onLine"),QVariant(false));
        QCOMPARE(evaluate("(()=>{try{const x=new XMLHttpRequest();x.open('GET','/data',false);x.send();return x.status}catch(e){return 'failed'}})()").toString(),QString("failed"));
        // The desktop shows the same setting.
        auto *network=object("pageNetwork");QVERIFY(network);QCOMPARE(network->property("currentText").toString(),QString("Offline"));
        // A bad value is refused and changes nothing.
        reply=previewCall("configure",{{"color_scheme","blue"}});
        QVERIFY(reply.value("error").toMap().value("message").toString().contains("color_scheme"));
        // The hidden inspector's debugger does not stop the page.
        QCOMPARE(evaluate("(()=>{debugger;return 7})()").toInt(),7);
        // Back to the desktop's own: the page is as before.
        reply=previewCall("configure",{{"color_scheme","system"},{"reduced_motion","system"},{"network",QVariantMap{{"preset","none"}}}});
        QVERIFY2(!reply.contains("error"),qPrintable(reply.value("error").toMap().value("message").toString()));
        QCOMPARE(evaluate("navigator.onLine"),QVariant(true));
        QCOMPARE(evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"),QVariant(false));
        measured=evaluate(timed).toList();QCOMPARE(measured.value(0).toInt(),200);QVERIFY2(measured.value(1).toInt()<380,qPrintable(measured.value(1).toString()));
        // The side panel sets it too.
        auto *dark=item("pageSchemeDark");QVERIFY(dark);
        QVERIFY(QMetaObject::invokeMethod(dark,"clicked"));
        QTRY_COMPARE(previewCall("state").value("result").toMap().value("color_scheme").toString(),QString("dark"));
        QTRY_COMPARE(evaluate("matchMedia('(prefers-color-scheme: dark)').matches"),QVariant(true));
        QVERIFY(QMetaObject::invokeMethod(item("pageSchemeSystem"),"clicked"));
        QTRY_COMPARE(evaluate("matchMedia('(prefers-color-scheme: dark)').matches"),QVariant(false));
        QVERIFY(previewCall("state").value("result").toMap().value("emulation").toMap().value("error").toString().isEmpty());
        previewCall("navigate",{{"url","tomacco://demo"}});window->setProperty("nativeMode",wasNative);
        // These calls are not the activity panel's business in the tests after this one.
        if(auto *panel=object("activityPanel"))panel->setProperty("calls",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void layout_lab_reports_the_page_loads_errors_and_failed_requests() {
        PageServer web("<!doctype html><title>errors</title><style>div{width:20px;height:20px;background:url(/missing-bg.png)}</style><div></div>"
                       "<img src=/missing.png alt=x><script>console.error('custom failure');console.warn('a warning');"
                       "fetch('/missing-api').then(r=>r.text());fetch('http://127.0.0.1:1/none').catch(()=>{});"
                       "Promise.reject(new Error('rejected promise'));setTimeout(()=>{throw new Error('thrown later')},0);"
                       "const x=new XMLHttpRequest();x.open('GET','/missing-xhr');x.send();</script>");
        QVERIFY(web.server.isListening());
        const bool wasNative=window->property("nativeMode").toBool();window->setProperty("nativeMode",false);
        QVERIFY(openPage(web.url()));
        QVariantMap report;
        QTRY_VERIFY2_WITH_TIMEOUT((report=previewCall("errors").value("result").toMap()).value("failed_requests").toList().size()>=5,qPrintable(QJsonDocument::fromVariant(report).toJson()),patient(10000));
        QVERIFY(report.value("collected").toBool());QVERIFY(!report.value("clean").toBool());
        QCOMPARE(report.value("url").toString(),web.url());
        const auto console=report.value("console_errors").toList();
        QVERIFY2(std::any_of(console.begin(),console.end(),[](const QVariant &e){return e.toMap().value("message").toString()=="custom failure";}),qPrintable(QJsonDocument::fromVariant(console).toJson()));
        QCOMPARE(report.value("console_warnings").toInt(),1);
        QStringList kinds;for(const auto &e:report.value("exceptions").toList())kinds<<e.toMap().value("kind").toString()+":"+e.toMap().value("message").toString();
        kinds.sort();QCOMPARE(kinds,(QStringList{"exception:thrown later","unhandled-rejection:rejected promise"}));
        // An uncaught error is listed once, as an exception, not again as a console error.
        for(const auto &e:console)QVERIFY(!e.toMap().value("message").toString().contains("thrown later"));
        QMap<QString,QVariantMap> failed;for(const auto &e:report.value("failed_requests").toList())failed[QUrl(e.toMap().value("url").toString()).path()]=e.toMap();
        QCOMPARE(failed.value("/missing-api").value("kind").toString(),QString("fetch"));QCOMPARE(failed.value("/missing-api").value("status").toInt(),404);
        QCOMPARE(failed.value("/missing-xhr").value("kind").toString(),QString("xhr"));QCOMPARE(failed.value("/missing-xhr").value("status").toInt(),404);
        QCOMPARE(failed.value("/missing.png").value("kind").toString(),QString("element"));QCOMPARE(failed.value("/missing.png").value("status").toInt(),404);
        QCOMPARE(failed.value("/missing-bg.png").value("kind").toString(),QString("resource"));QCOMPARE(failed.value("/missing-bg.png").value("status").toInt(),404);
        QCOMPARE(failed.value("/none").value("kind").toString(),QString("fetch"));QCOMPARE(failed.value("/none").value("status").toInt(),0);
        QVERIFY(!failed.value("/none").value("error").toString().isEmpty());
        // A new load starts a new report; a missing document is one failed request.
        QVERIFY(openPage(web.url("/missing-page")));
        QTRY_VERIFY_WITH_TIMEOUT(previewCall("errors").value("result").toMap().value("failed_requests").toList().size()==1,patient(5000));
        report=previewCall("errors").value("result").toMap();
        const auto document=report.value("failed_requests").toList().value(0).toMap();
        QCOMPARE(document.value("kind").toString(),QString("document"));QCOMPARE(document.value("status").toInt(),404);
        QVERIFY(report.value("exceptions").toList().isEmpty());QVERIFY(report.value("console_errors").toList().isEmpty());
        QVERIFY(openPage(web.url("/clean")));
        report=previewCall("errors").value("result").toMap();
        QVERIFY2(report.value("clean").toBool(),qPrintable(QJsonDocument::fromVariant(report).toJson()));
        previewCall("navigate",{{"url","tomacco://demo"}});window->setProperty("nativeMode",wasNative);
        if(auto *panel=object("activityPanel"))panel->setProperty("calls",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void layout_audit_finds_overflow_clipped_text_and_small_targets() {
        PageServer web("<!doctype html><meta name=viewport content='width=device-width'><title>audit</title>"
                       "<style>body{margin:0;font:16px sans-serif}#top{position:fixed;top:0;left:0;margin:0;width:100%}</style>"
                       "<p id=top>Under the island</p><div style='height:120px'></div>"
                       "<section><div style='width:2000px'>wide</div><div><button style='width:46px;height:46px'>Go</button></div></section>"
                       "<p>Read <a href='#more'>more</a> here</p><button id=big style='width:60px;height:60px'>Ok</button>");
        QVERIFY(web.server.isListening());
        const bool wasNative=window->property("nativeMode").toBool();window->setProperty("nativeMode",false);
        const auto before=previewCall("state").value("result").toMap();
        QVERIFY(!previewCall("configure",{{"device","18pro"},{"orientation","portrait"},{"safe_area",true}}).contains("error"));
        QVERIFY(openPage(web.url()));
        const auto audit=[&]{
            QProcess cli;cli.start(QCoreApplication::applicationDirPath()+"/tomaccoctl",{"call","preview.audit",R"({"session":"ui-acceptance"})"});
            QElapsedTimer clock;clock.start();
            while(cli.state()!=QProcess::NotRunning && clock.elapsed()<patient(30000))QTest::qWait(20);
            const auto reply=QJsonDocument::fromJson(cli.readAllStandardOutput()).object().toVariantMap();
            [&]{QVERIFY2(reply.value("ok").toBool(),qPrintable(QJsonDocument::fromVariant(reply).toJson()));}();
            QMultiMap<QString,QVariantMap> found;
            for(const auto &issue:reply.value("result").toMap().value("issues").toList())found.insert(issue.toMap().value("rule").toString(),issue.toMap());
            return std::make_pair(reply.value("result").toMap(),found);
        };
        auto [result,issues]=audit();
        QCOMPARE(result.value("platform").toString(),QString("ios"));QCOMPARE(result.value("target_minimum").toInt(),44);
        QVERIFY(issues.contains("document-overflow"));QVERIFY(issues.value("document-overflow").value("overflow").toInt()>1000);
        QCOMPARE(issues.value("horizontal-overflow").value("selector").toString(),QString("body > section > div:nth-of-type(1)"));
        // The fixed line at the top lies under the island: 62 points of inset on this profile.
        const auto clipped=issues.value("safe-area-text");
        QCOMPARE(clipped.value("selector").toString(),QString("#top"));QCOMPARE(clipped.value("edges").toList(),(QVariantList{"top"}));
        QCOMPARE(clipped.value("text").toString(),QString("Under the island"));
        // Selectors match the element they name, alone.
        for(const auto &issue:issues)QCOMPARE(evaluate("document.querySelectorAll("+QString::fromUtf8(QJsonDocument(QJsonArray{issue.value("selector").toString()}).toJson(QJsonDocument::Compact)).mid(1).chopped(1)+").length").toInt(),1);
        // 46 points is enough on iOS; the link in running text is exempt.
        QVERIFY(!issues.contains("target-size-platform"));
        QVERIFY(!previewCall("configure",{{"device","pixel-11"}}).contains("error"));
        QTRY_COMPARE(evaluate("window.__tomaccoViewport.platform").toString(),QString("android"));
        std::tie(result,issues)=audit();
        QCOMPARE(result.value("target_minimum").toInt(),48);
        const auto small=issues.values("target-size-platform");QCOMPARE(small.size(),1);
        QCOMPARE(small.value(0).value("selector").toString(),QString("body > section > div:nth-of-type(2) > button"));
        QCOMPARE(small.value(0).value("label").toString(),QString("Go"));QCOMPARE(small.value(0).value("width").toInt(),46);
        QVERIFY(!previewCall("configure",{{"device",before.value("device")},{"safe_area",before.value("safe_area")}}).contains("error"));
        previewCall("navigate",{{"url","tomacco://demo"}});window->setProperty("nativeMode",wasNative);
        if(auto *panel=object("activityPanel"))panel->setProperty("calls",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void iphone_frames_follow_the_measured_geometry() {
        // Every iPhone is drawn to scale from its profile: the body's millimetres through the panel's
        // density, the island (or notch, or punch hole) of its exact size at its exact place, the
        // buttons where Apple's drawing puts them, and the Duo folded and open.
        QQmlComponent component(engine.get(),QUrl("qrc:/qml/NativeDeviceFrame.qml"));
        QQuickWindow stage;stage.resize(1200,1000);stage.setColor(QColor("#14171e"));
        std::unique_ptr<QQuickItem> frame(qobject_cast<QQuickItem *>(component.create()));QVERIFY2(frame,qPrintable(component.errorString()));
        frame->setParentItem(stage.contentItem());frame->setProperty("roundedScreen",true);frame->setProperty("backgroundColor",QColor("transparent"));
        // A picture in the screen, as a live display would fill it.
        QQmlComponent pictureComponent(engine.get());
        pictureComponent.setData("import QtQuick\nRectangle {anchors.fill:parent;gradient:Gradient {GradientStop {position:0;color:\"#4b6cb7\"} GradientStop {position:1;color:\"#182848\"}}}",QUrl());
        std::unique_ptr<QQuickItem> picture(qobject_cast<QQuickItem *>(pictureComponent.create()));QVERIFY(picture);
        picture->setParentItem(frame->property("screen").value<QQuickItem *>());
        stage.show();QVERIFY(QTest::qWaitForWindowExposed(&stage));
        const auto real=[&](const char *name){return frame->property(name).toReal();};
        const auto near=[](qreal a,qreal b){return qAbs(a-b)<.01;};
        const auto child=[&](const QString &name){return visualItem(frame.get(),name);};
        const auto shoot=[&](const QString &name){
            const auto out=qEnvironmentVariable("TOMACCO_UI_CAPTURES");
            if(out.isEmpty())return;
            QTest::qWait(100);QVERIFY(QDir().mkpath(out));
            QVERIFY(stage.grabWindow().copy(QRect(0,0,qCeil(frame->width()),qCeil(frame->height()))).save(out+"/"+name+".png"));
        };
        const auto show=[&](const QVariantMap &profile,QSize size,bool unfolded,bool rear){
            frame->setProperty("profile",profile);frame->setProperty("unfolded",unfolded);
            frame->setProperty("sourceSize",size);frame->setProperty("rearView",rear);
            frame->setPosition(QPointF(8,8));
        };
        int iphones=0;
        for(const auto &entry:backend->models()) {
            const auto profile=entry.toMap();if(profile["platform"]!="ios")continue;
            ++iphones;
            const auto id=profile["id"].toString();
            const auto body=profile["body"].toMap();
            const qreal width=profile["width"].toReal(),height=profile["height"].toReal();
            // 460 ppi at three pixels a point: 6.037 points a millimetre.
            const qreal mm=profile["ppi"].toReal()/25.4/3;
            QVERIFY2(profile["ppi"].toReal()>400 && !body.isEmpty() && profile.contains("buttons") && profile.contains("back"),qPrintable(id));
            show(profile,QSize(qRound(width*3),qRound(height*3)),false,false);
            QVERIFY(!frame->property("turned").toBool());
            QVERIFY2(near(real("pointsPerMm"),mm),qPrintable(id));
            QVERIFY(near(real("bodyWidth"),body["width"].toReal()*mm));QVERIFY(near(real("bodyHeight"),body["height"].toReal()*mm));
            QVERIFY(near(real("screenY"),body.contains("screenTop")?body["screenTop"].toReal()*mm:(real("bodyHeight")-height)/2));
            QVERIFY(near(real("screenX"),body.contains("screenLeft")?body["screenLeft"].toReal()*mm:(real("bodyWidth")-width)/2));
            QCOMPARE(real("cornerRadius"),profile["radius"].toReal());
            auto *camera=child("nativeCameraCutout");QVERIFY(camera);QVERIFY(camera->isVisible());
            QCOMPARE(camera->width(),profile["cutoutWidth"].toReal());QCOMPARE(camera->height(),profile["cutoutHeight"].toReal());
            QVERIFY2(near(camera->y()-real("screenY"),profile["cutoutOffset"].toReal()),qPrintable(id));
            QVERIFY(near(camera->x()-real("screenX"),profile.contains("cutoutX")?profile["cutoutX"].toReal():(width-camera->width())/2));
            // The screen's picture sits exactly in the body's display opening.
            QVERIFY(near(frame->property("screenRect").toRectF().x(),real("screenX")+real("outerMargin")));
            QCOMPARE(frame->property("screenRect").toRectF().width(),width);
            // The body's corner is concentric with the screen's: about its radius plus the bezel, and
            // never tighter than the screen's, which would leave a black wedge in every corner. Apple's
            // drawings give 12.71 mm on the 18 Pro; the 13.90 mm point on that curve read as its tangent
            // gave 9.09 mm, 55 points around a 62-point screen.
            QVERIFY2(real("bodyRadius")>real("cornerRadius"),qPrintable(id));
            QVERIFY2(qAbs(real("bodyRadius")-real("cornerRadius")-real("bezel"))<8,qPrintable(id));
            // Each drawing's metal band, seen from the front: the housing's edge to the cover glass.
            const QHash<QString,qreal> bands{{"18pro",1.2},{"18promax",1.2},{"17",1.0},{"air",1.14},{"17e",1.05},{"16",1.0},{"duo",1.33}};
            QVERIFY2(qAbs(real("rail")/mm-bands.value(id))<.03,qPrintable(id));
            // The picture is the opening at every zoom: the same rectangle, the masked layer rendered at
            // the size it is shown, and with a live picture its edges on whole screen pixels.
            frame->setProperty("pixelAligned",true);
            for(const qreal zoom:{.5,.74,1.,1.2,1.5,2.,2.5,3.}) {
                frame->setProperty("displayScale",zoom);
                const auto opening=frame->property("screenRect").toRectF();
                QCOMPARE(opening.size(),QSizeF(width,height));
                QVERIFY2(near(opening.x(),real("screenX")+real("outerMargin")),qPrintable(id));
                QVERIFY2(qAbs(opening.x()*zoom-qRound(opening.x()*zoom))<.01 && qAbs(opening.y()*zoom-qRound(opening.y()*zoom))<.01,qPrintable(id));
                // Within half a screen pixel of the drawing.
                QVERIFY2(qAbs(real("screenX")-(body.contains("screenLeft")?body["screenLeft"].toReal()*mm:(real("bodyWidth")-width)/2))<=.5/zoom+.001,qPrintable(id));
                QCOMPARE(frame->property("screenTextureSize").toSizeF(),QSizeF(qCeil(width*zoom),qCeil(height*zoom)));
                const auto *screen=frame->property("screen").value<QQuickItem *>();
                QCOMPARE(picture->width(),screen->width());QCOMPARE(picture->height(),screen->height());
            }
            frame->setProperty("pixelAligned",false);frame->setProperty("displayScale",1);
            frame->setProperty("pictureHasCutout",true);
            QVERIFY(!camera->isVisible());
            frame->setProperty("pictureHasCutout",false);
            const auto buttons=profile["buttons"].toList();
            for(const auto &entry:buttons) {
                const auto button=entry.toMap();
                auto *item=child("nativeButton-"+button["kind"].toString());QVERIFY2(item,qPrintable(id+" "+button["kind"].toString()));
                const bool across=button["edge"]=="top" || button["edge"]=="bottom";
                QVERIFY(near(across?item->x():item->y(),button["from"].toReal()*(across?real("bodyWidth"):real("bodyHeight"))));
            }
            // No Action button on the Duo, no Camera Control on the 17e.
            QCOMPARE(!!child("nativeButton-action"),id!="duo");QCOMPARE(!!child("nativeButton-cameraControl"),id!="17e");
            shoot("frame-"+id);
            // From behind: the camera parts instead of the picture.
            show(profile,QSize(qRound(width*3),qRound(height*3)),false,true);
            QVERIFY(child("nativeDeviceBack")->isVisible());QVERIFY(!frame->property("screen").value<QQuickItem *>()->isVisible());QVERIFY(!camera->isVisible());
            shoot("frame-"+id+"-back");
            // Turned a quarter to the left the picture is landscape and the island is at the left.
            show(profile,QSize(qRound(height*3),qRound(width*3)),false,false);
            QVERIFY(frame->property("turned").toBool());
            QVERIFY(near(frame->property("screenRect").toRectF().width(),height));
            const auto island=camera->mapRectToItem(frame.get(),QRectF(0,0,camera->width(),camera->height()));
            QVERIFY2(island.center().x()<frame->width()/2,qPrintable(id));
            shoot("frame-"+id+"-landscape");
        }
        QCOMPARE(iphones,7);
        // The Duo: folded, a punch-hole camera top right (37 points across, 48 points in from the top
        // and right edges), the active area 4.13 mm from the hinge on the left, tight corners there.
        QVariantMap duo;for(const auto &p:backend->models())if(p.toMap()["id"]=="duo")duo=p.toMap();
        show(duo,QSize(1398,2034),false,false);
        QCOMPARE(frame->property("cutout").toString(),QString("punch"));
        const auto radii=frame->property("screenRadii").toList();
        QCOMPARE(radii.value(0).toReal(),8.0);QCOMPARE(radii.value(1).toReal(),59.0);QCOMPARE(radii.value(3).toReal(),8.0);
        auto *camera=child("nativeCameraCutout");
        QVERIFY(qAbs(camera->x()-real("screenX")+camera->width()/2-418.1)<.2);QVERIFY(qAbs(camera->y()-real("screenY")+camera->height()/2-47.75)<.2);
        QVERIFY(real("screenX")>real("bodyWidth")-466-real("screenX"));
        QVERIFY(child("nativeHinge")->isVisible());QVERIFY(child("nativeHinge")->x()<0);
        QCOMPARE(child("nativeButton-volumeUp")->y()<0,true);
        // Open: the inner display, 951 by 669 points at 430 ppi, wide in its own orientation, with no
        // cutout (its camera is under the display), the crease down the middle, buttons on the right half.
        show(duo,QSize(2670,1878),true,false);
        QVERIFY(!frame->property("turned").toBool());
        // 951 points across within the panel's own aspect (2670 by 1878 pixels).
        QVERIFY(qAbs(real("screenWidth")-951)<.2);QCOMPARE(real("screenHeight"),669.0);
        QVERIFY(!camera->isVisible());QCOMPARE(real("cornerRadius"),55.0);
        QVERIFY(near(real("pointsPerMm"),669/(1878/430.0*25.4)));
        QVERIFY(near(real("bodyWidth"),164.6*real("pointsPerMm")));
        auto *crease=child("nativeCrease");QVERIFY(crease->isVisible());
        QVERIFY(near(crease->x()+crease->width()/2,real("bodyWidth")/2));
        QVERIFY(!child("nativeHinge")->isVisible());
        QVERIFY(child("nativeButton-volumeDown")->x()>real("bodyWidth")/2);
        shoot("frame-duo-open");
        show(duo,QSize(2670,1878),true,true);
        shoot("frame-duo-open-back");
        // Upright, the open Duo turns: the crease runs across.
        show(duo,QSize(1878,2670),true,false);
        QVERIFY(frame->property("turned").toBool());QCOMPARE(frame->width(),real("bodyHeight")+real("outerMargin")*2);
        shoot("frame-duo-open-portrait");
        picture.reset();frame.reset();
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void ios_guest_picture_shows_the_phone_the_user_sees() {
        // The one-step phone runs as a model without a Home button (here the iPhone 16,
        // panel 1179x2556 at three pixels a point). Its picture must show that phone wherever it is
        // shown: docked with the island and the screen's corners, which iOS leaves to the hardware,
        // as tall as the dialog allows so the status bar survives the scaling, and in its own window
        // pixel for pixel when the monitor fits it. Without a model, the classic phone's points follow
        // its panel.
        open("ios-research");
        auto *view=object("iosResearch");QVERIFY(view);
        auto *dialog=object("iosResearchDialog");QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        QVariant virtualJob=QVariantMap{{"id","fixture-display"},{"state","running"},{"created",1240},
            {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.paravirtual"},
            {"arguments",QVariantMap{{"engine","/agent/virtual-engine"},{"interactive",true},{"seconds",600}}}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,virtualJob)));
        QVERIFY(item("iosGuestView")->isVisible());
        auto *guestView=object("iosGuestView");QVERIFY(guestView);
        auto *guestWindow=qobject_cast<QQuickWindow *>(object("iosGuestWindow"));QVERIFY(guestWindow);
        auto *picture=qobject_cast<QQuickItem *>(object("iosGuestFrame"));QVERIFY(picture);
        auto *contact=qobject_cast<QQuickItem *>(object("iosGuestContact"));QVERIFY(contact);
        auto *island=qobject_cast<QQuickItem *>(object("iosGuestIsland"));QVERIFY(island);
        auto *corners=qobject_cast<QQuickItem *>(object("iosGuestCorners"));QVERIFY(corners);
        auto *dock=qobject_cast<QQuickItem *>(object("iosGuestDock"));QVERIFY(dock);
        auto *modernFrame=qobject_cast<QQuickItem *>(object("iosGuestModernFrame"));QVERIFY(modernFrame);
        QVariantMap iphone;for(const auto &p:backend->models())if(p.toMap()["id"]=="16")iphone=p.toMap();
        QVERIFY(!iphone.isEmpty());
        // A frame of the panel's size, as the guest delivers it through the file transport.
        QImage frameImage(1179,2556,QImage::Format_RGB32);frameImage.fill(QColor("#8a7566"));
        QVERIFY(frameImage.save(directory.filePath("frame.png")));
        guestView->setProperty("detachedZoom",0);
        guestView->setProperty("running",true);guestView->setProperty("guest",QVariantMap{{"state",QVariantMap{{"running",true}}}});
        // The dialog binds the model and the dock height to the phone's polled status; a fixture
        // writes them through QQmlProperty so the next status reply cannot take them back.
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",iphone));
        guestView->setProperty("frameWidth",1179);guestView->setProperty("frameHeight",2556);
        guestView->setProperty("frameSource",QUrl::fromLocalFile(directory.filePath("frame.png")).toString());
        QTRY_VERIFY_WITH_TIMEOUT(contact->width()>0 && contact->height()>0,patient(5000));
        QCOMPARE(guestView->property("frameScale").toReal(),3.0);
        // Docked, the picture is the phone's 393-point screen at its painted scale, its corners covered.
        // iOS draws this model's island into the picture (372 pixels wide on the running guest), so a
        // second one here would stick out around it or under it.
        const qreal scale=contact->width()/393;
        QVERIFY(scale>0);
        QVERIFY(guestView->property("pictureDrawsCutout").toBool());
        QVERIFY(!island->isVisible());QVERIFY(corners->isVisible());
        for(const auto &entry:backend->models()) {
            const auto model=entry.toMap();if(model["platform"]!="ios")continue;
            QVERIFY(QQmlProperty::write(guestView,"deviceProfile",model));
            const bool drawn=model["cutout"]=="island" || model["cutout"]=="compact";
            QCOMPARE(guestView->property("pictureDrawsCutout").toBool(),drawn);
            // iOS draws no notch or punch hole: those stay.
            QCOMPARE(island->isVisible(),!drawn);
        }
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",iphone));
        // A profile that is not a guest's model has nothing drawn for it: the island sits centred 11.33
        // points below the top edge, 125 by 36.67 points (its bottom at 48 points).
        QVariantMap plain=iphone;plain.remove("virtual");
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",plain));
        QVERIFY(island->isVisible());
        QCOMPARE(island->width(),125*scale);QCOMPARE(island->height(),36.67*scale);
        QCOMPARE(island->y(),11.33*scale);QCOMPARE(island->x()+island->width()/2,contact->width()/2);
        QVERIFY(qAbs((island->y()+island->height())/scale*3-144)<.1);
        QCOMPARE(corners->width(),contact->width());QCOMPARE(corners->height(),contact->height());
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",iphone));
        QVERIFY(qAbs(contact->width()/contact->height()-1179.0/2556)<.002);
        capture("ios-guest-docked");
        // The docked picture takes the height the dialog offers, never less than before.
        QVERIFY(dock->height()>=460);
        QVERIFY(QQmlProperty::write(guestView,"dockHeight",720));
        QTRY_COMPARE_WITH_TIMEOUT(dock->height(),720.0,patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(contact->height()>600,patient(3000));
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",plain));
        QCOMPARE(island->width(),125*contact->width()/393);
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",iphone));
        QVERIFY(QQmlProperty::write(guestView,"dockHeight",0));
        QTRY_COMPARE_WITH_TIMEOUT(dock->height(),460.0,patient(3000));
        // The classic phone takes its points from the panel the guest was started with: 460 ppi
        // (1179 pixels on 65 mm) is three pixels a point, 326 ppi two, and three until it is known.
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",QVariantMap{}));
        QVERIFY(!island->isVisible());QVERIFY(!corners->isVisible());
        QCOMPARE(guestView->property("frameScale").toReal(),3.0);
        const auto classic=[&](int width,int widthMm){
            guestView->setProperty("guest",QVariantMap{{"state",QVariantMap{{"running",true}}},{"requested_display_profile",
                QVariantMap{{"width",width},{"height",width*2},{"refresh_hz",60},{"width_mm",widthMm},{"height_mm",widthMm*2}}}});
        };
        classic(1179,65);QCOMPARE(guestView->property("frameScale").toReal(),3.0);
        classic(750,58);QCOMPARE(guestView->property("frameScale").toReal(),2.0);
        guestView->setProperty("frameWidth",750);guestView->setProperty("frameHeight",1334);
        QCOMPARE(guestWindow->property("deviceWidth").toReal(),750/2.0+28);
        QCOMPARE(guestWindow->property("deviceHeight").toReal(),1334/2.0+146);
        guestView->setProperty("guest",QVariantMap{{"state",QVariantMap{{"running",true}}}});
        // In its own window the phone opens as sharp as the monitor allows, up to one panel pixel per
        // screen pixel, never enlarged beyond that, and keeps the zoom the user chooses. Two models
        // stand in for the user's monitors on the test screen: the phone itself (852 points, a
        // 2556-pixel frame) does not fit one panel pixel per screen pixel there, like the landscape
        // monitor; a small model does, like the portrait monitor.
        QVariantMap small=iphone;small["width"]=50;small["height"]=100;small["radius"]=10;
        const auto showModel=[&](const QVariantMap &model,int frameWidth,int frameHeight){
            QVERIFY(QQmlProperty::write(guestView,"deviceProfile",model));
            guestView->setProperty("frameWidth",frameWidth);guestView->setProperty("frameHeight",frameHeight);
            QCOMPARE(guestView->property("frameScale").toReal(),3.0);
        };
        const auto showPhone=[&]{showModel(iphone,1179,2556);};
        const auto showSmall=[&]{showModel(small,150,300);};
        const auto detach=[&](bool on){
            guestView->setProperty("detached",on);
            if(on)QTRY_VERIFY_WITH_TIMEOUT(guestWindow->isVisible(),patient(3000));else QTRY_VERIFY_WITH_TIMEOUT(!guestWindow->isVisible(),patient(3000));
        };
        const auto zoom=[&]{return guestWindow->property("zoom").toReal();};
        const auto kept=[&]{return guestView->property("detachedZoom").toReal();};
        // Zoom moves in steps of five percent and never beyond what fits.
        const auto fitting=[&]{const auto fit=guestWindow->property("fitZoom").toReal();return qMin(fit,std::round(qMin(3.0,fit)*20)/20);};
        // Where 1:1 does not fit, the window opens as large as fits. That automatic zoom is not kept
        // as the user's choice: the next open on a monitor that fits the panel is pixel-exact.
        showPhone();
        detach(true);
        QCOMPARE(guestWindow->property("maxZoom").toReal(),3.0);
        QVERIFY(guestWindow->property("fitZoom").toReal()<3.0);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),fitting(),patient(3000));
        QCOMPARE(kept(),0.0);
        // On a screen that fits more, the window opens at a clean ratio of screen pixels to
        // points (2, 1.5, 1): the 2.27 that fills a 2160-pixel monitor opens at 2, 1.7 at 1.5, a large
        // screen at the panel's own pixels; below one pixel per point as large as fits.
        const auto sharp=[&](double fit){QVariant zoom;QMetaObject::invokeMethod(guestView,"sharpZoom",Q_RETURN_ARG(QVariant,zoom),Q_ARG(QVariant,fit));return zoom.toReal();};
        QCOMPARE(sharp(2.27),2.0);QCOMPARE(sharp(1.7),1.5);QCOMPARE(sharp(3.5),3.0);QCOMPARE(sharp(1.2),1.0);QCOMPARE(sharp(0.8),0.8);
        detach(false);
        showSmall();
        detach(true);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),3.0,patient(3000));
        QCOMPARE(kept(),0.0);
        QCOMPARE(guestWindow->property("deviceWidth").toReal(),modernFrame->width());
        auto *host=guestWindow->findChild<QQuickItem *>("deviceWindowHost");QVERIFY(host);
        QCOMPARE(host->width(),qCeil(modernFrame->width()*3));
        QVERIFY(!island->isVisible());QVERIFY(!corners->isVisible());
        // The picture fills the frame's opening, on whole screen pixels, with no second island.
        QTRY_COMPARE_WITH_TIMEOUT(contact->width(),50.0,patient(3000));QCOMPARE(contact->height(),100.0);
        auto *opening=modernFrame->property("screen").value<QQuickItem *>();QVERIFY(opening);
        QCOMPARE(picture->parentItem(),opening);
        const auto corner=opening->mapToScene(QPointF(0,0));
        QVERIFY(qAbs(corner.x()-qRound(corner.x()))<.01 && qAbs(corner.y()-qRound(corner.y()))<.01);
        QVERIFY(modernFrame->property("pictureHasCutout").toBool());
        QVERIFY(!visualItem(modernFrame,"nativeCameraCutout")->isVisible());
        QVERIFY(QMetaObject::invokeMethod(guestWindow,"setZoom",Q_ARG(QVariant,4)));
        QCOMPARE(zoom(),3.0);
        QCOMPARE(kept(),0.0);
        // What the user chooses is kept: with Ctrl+minus, Ctrl+wheel and the rail's plus.
        guestWindow->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(guestWindow->isActive(),patient(3000));
        QTest::keyClick(guestWindow,Qt::Key_Minus,Qt::ControlModifier);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),2.9,patient(3000));QCOMPARE(kept(),2.9);
        const auto centre=contact->mapToScene(QPointF(contact->width()/2,contact->height()/2));
        QWheelEvent grow(centre,QPointF(guestWindow->mapToGlobal(centre.toPoint())),{},QPoint(0,120),Qt::NoButton,Qt::ControlModifier,Qt::NoScrollPhase,false);
        QCoreApplication::sendEvent(guestWindow,&grow);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),2.95,patient(3000));QCOMPARE(kept(),2.95);
        auto *zoomIn=guestWindow->findChild<QQuickItem *>("deviceWindowZoomIn");QVERIFY(zoomIn);
        QTest::mouseClick(guestWindow,Qt::LeftButton,Qt::NoModifier,zoomIn->mapToScene(QPointF(zoomIn->width()/2,zoomIn->height()/2)).toPoint());
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),3.0,patient(3000));QCOMPARE(kept(),3.0);
        // Where the chosen zoom does not fit the window shrinks to what fits (the test screen is the
        // only one, so the panel grows instead of the monitor shrinking), and that clamp does not
        // replace the choice: opened again where it fits, the window comes up at the choice.
        guestView->setProperty("frameHeight",1500);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),fitting(),patient(3000));
        QVERIFY(zoom()<3.0);
        QCOMPARE(kept(),3.0);
        detach(false);
        showSmall();
        detach(true);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),3.0,patient(3000));
        // A choice below the pixel-exact zoom is restored on the next open as well.
        QVERIFY(QMetaObject::invokeMethod(guestWindow,"chooseZoom",Q_ARG(QVariant,2)));
        QCOMPARE(zoom(),2.0);QCOMPARE(kept(),2.0);
        if(!qEnvironmentVariable("TOMACCO_UI_CAPTURES").isEmpty())QVERIFY(guestWindow->grabWindow().save(qEnvironmentVariable("TOMACCO_UI_CAPTURES")+"/ios-guest-window.png"));
        detach(false);
        detach(true);
        QTRY_COMPARE_WITH_TIMEOUT(zoom(),2.0,patient(3000));
        guestView->setProperty("detachedZoom",0);
        guestView->setProperty("detached",false);QVERIFY(QQmlProperty::write(guestView,"deviceProfile",QVariantMap{}));
        QTRY_VERIFY_WITH_TIMEOUT(!guestWindow->isVisible(),patient(3000));
        guestView->setProperty("running",false);guestView->setProperty("frameWidth",0);guestView->setProperty("frameHeight",0);guestView->setProperty("frameSource","");
        window->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(window->isActive(),patient(3000));
        view->setProperty("jobId","");view->setProperty("mode","ios");
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void ios_guest_touch_follows_the_finger_as_it_moves() {
        // Press, moves and release reach the guest as they happen, one touch request at a time with the
        // moves in between coalesced; a press without movement is the press and release of a click.
        // The scenario answers each touch request itself, before the real control plane can.
        open("ios-research");
        auto *view=object("iosResearch");QVERIFY(view);
        auto *dialog=object("iosResearchDialog");QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        const QString job="0123456789abcdef0123456789abcdef";
        QVariant virtualJob=QVariantMap{{"id",job},{"state","running"},{"created",1241},
            {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.paravirtual"},
            {"arguments",QVariantMap{{"engine","/agent/virtual-engine"},{"interactive",true},{"seconds",600}}}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,virtualJob)));
        auto *guestView=object("iosGuestView");QVERIFY(guestView);
        auto *contact=qobject_cast<QQuickItem *>(object("iosGuestContact"));QVERIFY(contact);
        QImage frameImage(390,844,QImage::Format_RGB32);frameImage.fill(QColor("#3d5a6c"));
        QVERIFY(frameImage.save(directory.filePath("touch-frame.png")));
        guestView->setProperty("running",true);guestView->setProperty("guest",QVariantMap{{"state",QVariantMap{{"running",true}}}});
        guestView->setProperty("frameWidth",390);guestView->setProperty("frameHeight",844);
        guestView->setProperty("frameSource",QUrl::fromLocalFile(directory.filePath("touch-frame.png")).toString());
        QTRY_VERIFY_WITH_TIMEOUT(contact->isEnabled() && contact->width()>100 && contact->height()>100,patient(5000));
        guestView->setProperty("liveTouch",true);
        auto *surface=contact->window();QVERIFY(surface);
        const auto at=[&](qreal x,qreal y){return contact->mapToScene(QPointF(contact->width()*x,contact->height()*y)).toPoint();};
        // The touch request in flight, answered as the control plane would.
        const auto inFlight=[&]{
            const int id=guestView->property("touchPending").toInt();
            return std::pair{id,mapOf(guestView->property("touchRequests")).value(QString::number(id)).toMap()};
        };
        const auto answer=[&](const QVariantMap &error={}){
            const auto [id,event]=inFlight();
            if(id)control->apiFinished(id,"ios.emulator.control",error.isEmpty()?QVariantMap{{"ok",true},{"result",QVariantMap{}}}:QVariantMap{{"ok",false},{"error",error}});
            return event;
        };
        // Sends the next touch request without waiting out the pace between them, and answers it: the
        // event loop never runs between the two, so the real control plane cannot answer first.
        auto *pace=object("iosGuestTouchPace");QVERIFY(pace);
        const auto next=[&](const QVariantMap &error={}){
            if(!inFlight().first){QMetaObject::invokeMethod(pace,"stop");QMetaObject::invokeMethod(guestView,"pumpTouch");}
            return answer(error);
        };
        const auto queued=[&]{return guestView->property("touchQueue").toList();};
        const auto played=[&](const QString &action){
            for(const auto &input:guestView->property("inputQueue").toList())if(input.toMap().value("action")==action)return true;
            const auto all=mapOf(guestView->property("requests"));
            for(const auto &context:all)if(context.toMap().value("action")==action)return true;
            return false;
        };
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("pending").toInt(),0,patient(8000));
        // A swipe: the press goes out at once, before any movement.
        QTest::mousePress(surface,Qt::LeftButton,{},at(.5,.6));
        auto event=answer();
        QCOMPARE(event.value("action").toString(),QString("touch_down"));
        QCOMPARE(event.value("job").toString(),job);
        QVERIFY(qAbs(event.value("y").toReal()-.6)<.01);
        // A few pixels are still the press point.
        QTest::mouseMove(surface,at(.5,.6)+QPoint(2,2));
        QTest::qWait(30);
        QVERIFY(queued().isEmpty());QCOMPARE(inFlight().first,0);
        QTest::mouseMove(surface,at(.5,.5));
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_move"));
        QVERIFY(qAbs(event.value("y").toReal()-.5)<.01);
        // Moves made while one waits are kept as the latest point only.
        QTest::mouseMove(surface,at(.5,.45));QTest::mouseMove(surface,at(.5,.4));QTest::mouseMove(surface,at(.5,.3));
        QCOMPARE(queued().size(),1);
        QVERIFY(qAbs(queued()[0].toMap().value("y").toReal()-.3)<.01);
        answer();
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_move"));
        QVERIFY(qAbs(event.value("y").toReal()-.3)<.01);
        QTest::mouseRelease(surface,Qt::LeftButton,{},at(.5,.2));
        QStringList actions;QVariantMap last;
        while(actions.isEmpty() || actions.last()!="touch_up") {last=next();actions.append(last.value("action").toString());}
        QVERIFY(qAbs(last.value("y").toReal()-.2)<.01);
        QCOMPARE(actions.count("touch_up"),1);
        QVERIFY(!played("drag"));QVERIFY(!played("click"));
        // A tap: press and release at the press point, nothing played back afterwards.
        QTest::mouseClick(surface,Qt::LeftButton,{},at(.3,.3));
        event=next();QCOMPARE(event.value("action").toString(),QString("touch_down"));
        const auto pressed=event;
        event=next();QCOMPARE(event.value("action").toString(),QString("touch_up"));
        QCOMPARE(event.value("x").toReal(),pressed.value("x").toReal());QCOMPARE(event.value("y").toReal(),pressed.value("y").toReal());
        QVERIFY(!played("click"));
        // A lost grab lifts the finger while the button is still down, and the release plays nothing back.
        QTest::mousePress(surface,Qt::LeftButton,{},at(.4,.4));
        next();
        QVERIFY(QMetaObject::invokeMethod(contact,"canceled"));
        event=next();QCOMPARE(event.value("action").toString(),QString("touch_up"));
        QTest::mouseRelease(surface,Qt::LeftButton,{},at(.4,.4));
        QTest::qWait(30);
        QCOMPARE(inFlight().first,0);QVERIFY(queued().isEmpty());QVERIFY(!played("click"));
        // A control plane without the lane refuses the press: the release is still sent, the gesture
        // is played back as a click, and later gestures go out after release as before.
        QTest::mousePress(surface,Qt::LeftButton,{},at(.6,.6));
        next({{"code","invalid_argument"},{"message","arguments.action must be one of …"}});
        QVERIFY(!guestView->property("liveTouch").toBool());
        QTest::mouseRelease(surface,Qt::LeftButton,{},at(.6,.6));
        event=next();QCOMPARE(event.value("action").toString(),QString("touch_up"));
        QVERIFY(played("click"));
        guestView->setProperty("inputQueue",QVariantList{});
        QTest::mousePress(surface,Qt::LeftButton,{},at(.5,.7));
        QTest::mouseMove(surface,at(.5,.5));QTest::mouseMove(surface,at(.5,.3));
        QTest::mouseRelease(surface,Qt::LeftButton,{},at(.5,.3));
        QTest::qWait(30);
        QCOMPARE(inFlight().first,0);QVERIFY(queued().isEmpty());
        QVERIFY(played("drag"));
        guestView->setProperty("inputQueue",QVariantList{});guestView->setProperty("liveTouch",true);
        guestView->setProperty("running",false);guestView->setProperty("frameWidth",0);guestView->setProperty("frameHeight",0);guestView->setProperty("frameSource","");
        view->setProperty("jobId","");view->setProperty("mode","ios");
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void ios_guest_edge_swipes_start_at_the_edge() {
        // iOS opens Home, the app switcher, both centres and back only for a touch that starts at the
        // screen's very edge. A press close to an edge, or on the frame just around the picture, starts
        // there, and its moves follow at once; the mark stays under the pointer. Shortcuts and buttons
        // play the system gestures as one drag each.
        open("ios-research");
        auto *view=object("iosResearch");QVERIFY(view);
        auto *dialog=object("iosResearchDialog");QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        const QString job="fedcba9876543210fedcba9876543210";
        QVariant virtualJob=QVariantMap{{"id",job},{"state","running"},{"created",1242},
            {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.paravirtual"},
            {"arguments",QVariantMap{{"engine","/agent/virtual-engine"},{"interactive",true},{"seconds",600}}}}}}}};
        QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,virtualJob)));
        auto *guestView=object("iosGuestView");QVERIFY(guestView);
        auto *contact=qobject_cast<QQuickItem *>(object("iosGuestContact"));QVERIFY(contact);
        auto *mark=qobject_cast<QQuickItem *>(object("iosGuestTouchMark"));QVERIFY(mark);
        QImage frameImage(390,844,QImage::Format_RGB32);frameImage.fill(QColor("#4a5d3c"));
        QVERIFY(frameImage.save(directory.filePath("edge-frame.png")));
        guestView->setProperty("running",true);guestView->setProperty("guest",QVariantMap{{"state",QVariantMap{{"running",true}}}});
        guestView->setProperty("frameWidth",390);guestView->setProperty("frameHeight",844);
        guestView->setProperty("frameSource",QUrl::fromLocalFile(directory.filePath("edge-frame.png")).toString());
        QTRY_VERIFY_WITH_TIMEOUT(contact->isEnabled() && contact->width()>100 && contact->height()>100,patient(5000));
        guestView->setProperty("liveTouch",true);
        // The dialog's column has placed what follows the device below it, so nothing covers the
        // picture's bottom edge.
        auto *guestItem=qobject_cast<QQuickItem *>(guestView);QVERIFY(guestItem);
        const auto laidOut=[&]{
            const auto siblings=guestItem->parentItem()->childItems();
            for(auto i=siblings.indexOf(guestItem)+1;i<siblings.size();++i)
                if(siblings[i]->isVisible() && siblings[i]->y()<guestItem->y()+guestItem->height()-.5)return false;
            return true;
        };
        QTRY_VERIFY_WITH_TIMEOUT(laidOut(),patient(3000));
        auto *surface=contact->window();QVERIFY(surface);
        // A point given in the picture's own pixels, which may lie on the frame around it.
        const auto px=[&](qreal x,qreal y){return contact->mapToScene(QPointF(x,y)).toPoint();};
        const qreal w=contact->width(),h=contact->height();
        const auto inFlight=[&]{
            const int id=guestView->property("touchPending").toInt();
            return std::pair{id,mapOf(guestView->property("touchRequests")).value(QString::number(id)).toMap()};
        };
        auto *pace=object("iosGuestTouchPace");QVERIFY(pace);
        const auto next=[&]{
            if(!inFlight().first){QMetaObject::invokeMethod(pace,"stop");QMetaObject::invokeMethod(guestView,"pumpTouch");}
            const auto [id,event]=inFlight();
            if(id)control->apiFinished(id,"ios.emulator.control",QVariantMap{{"ok",true},{"result",QVariantMap{}}});
            return event;
        };
        const auto finish=[&]{
            QStringList actions;QVariantMap last;
            while(actions.isEmpty() || actions.last()!="touch_up") {last=next();if(last.isEmpty())return QVariantMap{};actions.append(last.value("action").toString());}
            return last;
        };
        const auto markAt=[&](qreal x,qreal y){
            return qAbs(mark->x()+mark->width()/2-x)<1.5 && qAbs(mark->y()+mark->height()/2-y)<1.5;
        };
        const auto playedAnything=[&]{
            if(!guestView->property("inputQueue").toList().isEmpty())return true;
            for(const auto &context:mapOf(guestView->property("requests"))){const auto a=context.toMap().value("action");if(a=="click" || a=="drag")return true;}
            return false;
        };
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("pending").toInt(),0,patient(8000));
        // Two pixels above the bottom edge: the finger starts on it.
        QTest::mousePress(surface,Qt::LeftButton,{},px(w/2,h-2));
        auto event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_down"));
        QCOMPARE(event.value("y").toReal(),1.0);
        QVERIFY(qAbs(event.value("x").toReal()-.5)<.01);
        QVERIFY(markAt(w/2,h-2));
        // An edge swipe follows from its first movement; the slop of a tap does not hold it back.
        QTest::mouseMove(surface,px(w/2,h-4));
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_move"));
        QVERIFY(event.value("y").toReal()<1.0);
        // A flick: the release goes out right behind the last move, where the pointer let go.
        QTest::mouseMove(surface,px(w/2,h*.7));QTest::mouseMove(surface,px(w/2,h*.5));
        QTest::mouseRelease(surface,Qt::LeftButton,{},px(w/2,h*.4));
        event=finish();
        QVERIFY(qAbs(event.value("y").toReal()-.4)<.01);
        QVERIFY(!playedAnything());
        // Pressed on the frame below the picture, the finger starts on the bottom edge and the mark
        // stays under the pointer; dragged in, the finger follows into the screen.
        auto *band=qobject_cast<QQuickItem *>(object("iosGuestEdgeBand"));QVERIFY(band);
        QVERIFY(band->height()>h+8);
        QTest::mousePress(surface,Qt::LeftButton,{},px(w*.3,h+5));
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_down"));
        QCOMPARE(event.value("y").toReal(),1.0);QVERIFY(qAbs(event.value("x").toReal()-.3)<.01);
        QVERIFY(markAt(w*.3,h+5));
        QTest::mouseMove(surface,px(w*.3,h*.7));
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_move"));
        QVERIFY(qAbs(event.value("y").toReal()-.7)<.01);
        QTest::mouseRelease(surface,Qt::LeftButton,{},px(w*.3,h*.7));
        event=finish();QVERIFY(qAbs(event.value("y").toReal()-.7)<.01);
        // Left of the picture: the left edge, for back. Held still, it lifts where it went down.
        QTest::mousePress(surface,Qt::LeftButton,{},px(-4,h/2));
        event=next();
        QCOMPARE(event.value("x").toReal(),0.0);QVERIFY(qAbs(event.value("y").toReal()-.5)<.01);
        QTest::mouseRelease(surface,Qt::LeftButton,{},px(-4,h/2));
        event=finish();QCOMPARE(event.value("x").toReal(),0.0);
        QVERIFY(!playedAnything());
        // Beyond the top-right corner of the picture: both edges, where Control Center opens.
        QTest::mousePress(surface,Qt::LeftButton,{},px(w+3,-3));
        event=next();
        QCOMPARE(event.value("x").toReal(),1.0);QCOMPARE(event.value("y").toReal(),0.0);
        QTest::mouseRelease(surface,Qt::LeftButton,{},px(w+3,-3));
        QCOMPARE(finish().value("action").toString(),QString("touch_up"));
        // Away from the edges a press is where it is, and a tap's few pixels of slop still hold.
        QTest::mousePress(surface,Qt::LeftButton,{},px(24,h*.4));
        event=next();
        QVERIFY(qAbs(event.value("x").toReal()-24/w)<.01);QVERIFY(qAbs(event.value("y").toReal()-.4)<.01);
        QTest::mouseMove(surface,px(26,h*.4+2));
        QTest::qWait(30);
        QCOMPARE(inFlight().first,0);QVERIFY(guestView->property("touchQueue").toList().isEmpty());
        QTest::mouseRelease(surface,Qt::LeftButton,{},px(26,h*.4+2));
        event=finish();QVERIFY(qAbs(event.value("x").toReal()-24/w)<.01);
        // Played back after release (a control plane without the lane), a press at the edge still
        // starts there.
        guestView->setProperty("liveTouch",false);
        guestView->setProperty("pending",987654);
        QTest::mousePress(surface,Qt::LeftButton,{},px(w/2,h+4));
        QTest::mouseMove(surface,px(w/2,h*.6));
        QTest::mouseRelease(surface,Qt::LeftButton,{},px(w/2,h*.5));
        auto inputs=guestView->property("inputQueue").toList();
        QCOMPARE(inputs.size(),1);
        auto played=inputs[0].toMap();
        QCOMPARE(played.value("action").toString(),QString("drag"));
        QCOMPARE(played.value("args").toMap().value("y").toReal(),1.0);
        QVERIFY(qAbs(played.value("args").toMap().value("to_y").toReal()-.5)<.01);
        guestView->setProperty("inputQueue",QVariantList{});guestView->setProperty("liveTouch",true);
        // The system gestures of a device without a Home button: Xcode's shortcuts with Ctrl for
        // Command, and a button each, every one a drag from the very edge. The lane is kept busy so
        // the drags wait in the queue to be read.
        QVariantMap iphone;for(const auto &p:backend->models())if(p.toMap()["id"]=="18pro")iphone=p.toMap();
        QVERIFY(!iphone.isEmpty());
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",iphone));
        QVERIFY(QQmlProperty::write(guestView,"guest",QVariantMap{{"state",QVariantMap{{"running",true}}},{"input_transport","usb-hid"},{"power_button_supported",true}}));
        window->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(window->isActive(),patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(guestView->property("shortcutsActive").toBool(),patient(3000));
        // The keys pressed on the way to a shortcut (Ctrl, Shift) still reach the guest as keys.
        const auto take=[&]{
            QVariantList queue;
            for(const auto &input:guestView->property("inputQueue").toList())if(input.toMap().value("action")!="key")queue.append(input);
            guestView->setProperty("inputQueue",QVariantList{});
            return queue;
        };
        const auto gesture=[&](Qt::Key key){
            QTest::keyClick(window,key,Qt::ControlModifier|Qt::ShiftModifier);
            const auto queue=take();
            if(queue.size()!=1 || queue[0].toMap().value("action")!="drag")return QVariantMap{};
            return queue[0].toMap().value("args").toMap();
        };
        auto drag=gesture(Qt::Key_A);
        QCOMPARE(drag.value("y").toReal(),1.0);QCOMPARE(drag.value("x").toReal(),.5);
        QVERIFY(drag.value("to_y").toReal()>.4 && drag.value("to_y").toReal()<.7);
        QVERIFY(drag.value("hold_ms").toInt()>=500 && drag.value("hold_ms").toInt()<=1000);
        QVERIFY(drag.value("duration_ms").toInt()>=300 && drag.value("duration_ms").toInt()<=2000);
        drag=gesture(Qt::Key_N);
        QCOMPARE(drag.value("y").toReal(),0.0);QVERIFY(drag.value("x").toReal()<.35);
        QVERIFY(drag.value("to_y").toReal()>.4);QCOMPARE(drag.value("hold_ms").toInt(),0);
        drag=gesture(Qt::Key_C);
        QCOMPARE(drag.value("y").toReal(),0.0);QVERIFY(drag.value("x").toReal()>.65);
        QVERIFY(drag.value("to_y").toReal()>.4);QCOMPARE(drag.value("hold_ms").toInt(),0);
        drag=gesture(Qt::Key_B);
        QCOMPARE(drag.value("x").toReal(),0.0);QVERIFY(drag.value("to_x").toReal()>.5);
        QCOMPARE(drag.value("y").toReal(),drag.value("to_y").toReal());
        // Home: the Home usage and the swipe up; the side button locks.
        QTest::keyClick(window,Qt::Key_H,Qt::ControlModifier|Qt::ShiftModifier);
        inputs=take();
        QCOMPARE(inputs.size(),2);
        QCOMPARE(inputs[0].toMap().value("action").toString(),QString("home"));
        QCOMPARE(inputs[1].toMap().value("action").toString(),QString("drag"));
        QTest::keyClick(window,Qt::Key_L,Qt::ControlModifier|Qt::ShiftModifier);
        inputs=take();
        QCOMPARE(inputs.size(),1);QCOMPARE(inputs[0].toMap().value("action").toString(),QString("power_button"));
        // The buttons play the same drags and name their shortcuts.
        auto *controlCenter=item("iosGuestGesture_controlCenter");QVERIFY(controlCenter);
        QTRY_VERIFY_WITH_TIMEOUT(controlCenter->isVisible() && controlCenter->width()>0,patient(3000));
        QVERIFY(controlCenter->property("tip").toString().endsWith("Ctrl+Shift+C"));
        QVERIFY(object("iosGuestHome")->property("tip").toString().endsWith("Ctrl+Shift+H"));
        QVERIFY(object("iosGuestPower")->property("tip").toString().endsWith("Ctrl+Shift+L"));
        QVERIFY(QMetaObject::invokeMethod(controlCenter,"clicked"));
        inputs=take();
        QCOMPARE(inputs.size(),1);
        QCOMPARE(inputs[0].toMap().value("args").toMap(),guestView->property("systemGestures").toMap().value("controlCenter").toMap());
        // A device with a Home button has none of them.
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",QVariantMap{}));
        QTest::keyClick(window,Qt::Key_A,Qt::ControlModifier|Qt::ShiftModifier);
        QVERIFY(take().isEmpty());
        QVERIFY(!controlCenter->isVisible());
        // In the model's own window the bezel around the screen is the screen's edge: a press there
        // starts on the edge instead of moving the window.
        QVERIFY(QQmlProperty::write(guestView,"deviceProfile",iphone));
        guestView->setProperty("frameWidth",1206);guestView->setProperty("frameHeight",2622);
        guestView->setProperty("detached",true);
        auto *guestWindow=qobject_cast<QQuickWindow *>(object("iosGuestWindow"));QVERIFY(guestWindow);
        auto *modernFrame=qobject_cast<QQuickItem *>(object("iosGuestModernFrame"));QVERIFY(modernFrame);
        QTRY_VERIFY_WITH_TIMEOUT(guestWindow->isVisible() && modernFrame->isVisible(),patient(3000));
        QTRY_VERIFY_WITH_TIMEOUT(contact->window()==guestWindow && contact->width()>100,patient(3000));
        QCOMPARE(modernFrame->property("screenReach").toReal(),contact->property("edgeReach").toReal());
        // Its rail goes back with the left-edge swipe and names the shortcuts.
        auto *back=guestWindow->findChild<QQuickItem *>("deviceWindowBack");QVERIFY(back);
        QVERIFY(back->isVisible());QVERIFY(back->property("tip").toString().endsWith("Ctrl+Shift+B"));
        QVERIFY(guestWindow->findChild<QQuickItem *>("deviceWindowHome")->property("tip").toString().endsWith("Ctrl+Shift+H"));
        QVERIFY(QMetaObject::invokeMethod(back,"clicked"));
        inputs=take();
        QCOMPARE(inputs.size(),1);
        QCOMPARE(inputs[0].toMap().value("args").toMap(),guestView->property("systemGestures").toMap().value("back").toMap());
        QVERIFY(contact->property("edgeReach").toReal()>0);
        guestView->setProperty("pending",0);
        // Halfway into the bezel below the screen, in the frame's units.
        const qreal bezel=modernFrame->property("bezel").toReal();
        QTest::mousePress(guestWindow,Qt::LeftButton,{},contact->mapToScene(QPointF(contact->width()*.5,contact->height()+bezel/2)).toPoint());
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_down"));
        QCOMPARE(event.value("y").toReal(),1.0);
        QTest::mouseMove(guestWindow,contact->mapToScene(QPointF(contact->width()*.5,contact->height()*.6)).toPoint());
        event=next();
        QCOMPARE(event.value("action").toString(),QString("touch_move"));
        QVERIFY(qAbs(event.value("y").toReal()-.6)<.02);
        QTest::mouseRelease(guestWindow,Qt::LeftButton,{},contact->mapToScene(QPointF(contact->width()*.5,contact->height()*.6)).toPoint());
        QCOMPARE(finish().value("action").toString(),QString("touch_up"));
        guestView->setProperty("inputQueue",QVariantList{});
        guestView->setProperty("detached",false);QVERIFY(QQmlProperty::write(guestView,"deviceProfile",QVariantMap{}));
        QTRY_VERIFY_WITH_TIMEOUT(!guestWindow->isVisible(),patient(3000));
        QTRY_COMPARE_WITH_TIMEOUT(guestView->property("pending").toInt(),0,patient(8000));
        guestView->setProperty("running",false);guestView->setProperty("frameWidth",0);guestView->setProperty("frameHeight",0);guestView->setProperty("frameSource","");
        window->requestActivate();QTRY_VERIFY_WITH_TIMEOUT(window->isActive(),patient(3000));
        view->setProperty("jobId","");view->setProperty("mode","ios");
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void a_lost_touch_lane_never_leaves_the_finger_down() {
        // A stand-in tomaccoctl: it answers the persistent lane, logs every touch it receives, and dies
        // on a move to x=1, as a crashed or killed child would, holding the contact down.
        const auto script=directory.filePath("touch-tomaccoctl");
        const auto log=directory.filePath("touch-tomaccoctl.log");
        {QFile file(script);QVERIFY(file.open(QIODevice::WriteOnly));file.write(R"(#!/usr/bin/env python3
import json, os, sys
log = open(os.environ["TOMACCO_FAKE_TOUCH_LOG"], "a")
def note(mode, value):
    log.write(json.dumps({"mode": mode, **value}) + "\n"); log.flush()
if sys.argv[1] == "schema":
    print(json.dumps({"ok": True, "result": {"version": "fake", "operations": []}})); sys.exit()
if sys.argv[1] == "call":
    note("call", json.load(sys.stdin)); print(json.dumps({"ok": True, "result": {}})); sys.exit()
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        reply = {"protocolVersion": "2025-11-25", "capabilities": {}}
    elif message.get("method") == "tools/call":
        arguments = message["params"]["arguments"]
        note("lane", arguments)
        if arguments.get("x") == 1:
            os._exit(3)
        reply = {"content": [{"type": "text", "text": json.dumps({"action": arguments["action"]})}], "isError": False}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": reply}), flush=True)
)");}
        QFile::setPermissions(script,QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner);
        qputenv("TOMACCO_FAKE_TOUCH_LOG",log.toUtf8());
        const auto entries=[&]{
            QList<QVariantMap> lines;QFile file(log);
            if(file.open(QIODevice::ReadOnly))for(const auto &line:file.readAll().split('\n'))if(!line.isEmpty())lines.append(QJsonDocument::fromJson(line).toVariant().toMap());
            return lines;
        };
        const auto lifts=[&]{int count=0;for(const auto &entry:entries())if(entry["mode"]=="call" && entry["action"]=="touch_up")++count;return count;};
        const QString job="fedcba9876543210fedcba9876543210";
        const auto touch=[&](const QString &action,double x){return QVariantMap{{"job_id",job},{"action",action},{"x",x},{"y",.5}};};
        {
            Control lane("ui-touch-lane",nullptr,script);
            QSignalSpy replies(&lane,&Control::apiFinished);
            // Frames and touches share the persistent child; a touch is answered there.
            const int down=lane.request("ios.emulator.control",touch("touch_down",.5));
            QTRY_VERIFY_WITH_TIMEOUT(std::any_of(replies.begin(),replies.end(),[&](const auto &r){return r.at(0).toInt()==down;}),patient(10000));
            QCOMPARE(entries().last()["mode"].toString(),QString("lane"));
            // The child dies holding the contact: a one-shot release lifts it.
            lane.request("ios.emulator.control",touch("touch_move",1));
            QTRY_COMPARE_WITH_TIMEOUT(lifts(),1,patient(10000));
            QCOMPARE(entries().last()["job_id"].toString(),job);
            // A contact released through the lane needs nothing more.
            lane.request("ios.emulator.control",touch("touch_down",.4));
            const int up=lane.request("ios.emulator.control",touch("touch_up",.4));
            QTRY_VERIFY_WITH_TIMEOUT(std::any_of(replies.begin(),replies.end(),[&](const auto &r){return r.at(0).toInt()==up;}),patient(10000));
            // The app closes while a finger is down.
            lane.request("ios.emulator.control",touch("touch_down",.3));
            QTRY_VERIFY_WITH_TIMEOUT(entries().last()["action"]=="touch_down" && entries().last()["x"].toDouble()==.3,patient(10000));
            QCOMPARE(lifts(),1);
        }
        QCOMPARE(lifts(),2);
        qunsetenv("TOMACCO_FAKE_TOUCH_LOG");
    }
    void ios_dialog_stop_always_leaves_a_way_to_end_the_guest() {
        auto *view=object("iosResearch");QVERIFY(view);
        auto *stop=qobject_cast<QQuickItem *>(object("iosResearchStop"));QVERIFY(stop);
        // The scenario answers every request itself, and no report poll may change the job underneath it.
        view->setProperty("polling",true);
        const auto sent=[&](const QString &operation,const QString &job){
            QList<int> ids;const auto all=mapOf(view->property("requests"));
            for(auto it=all.begin();it!=all.end();++it) {
                const auto context=it.value().toMap();
                if(context.value("operation").toString()==operation && context.value("jobId").toString()==job)ids.append(it.key().toInt());
            }
            return ids;
        };
        // The control plane's answer: the given error, or success.
        const auto answer=[&](int id,const QString &operation,const QVariantMap &error={}){
            control->apiFinished(id,operation,error.isEmpty()?QVariantMap{{"ok",true},{"result",QVariantMap{}}}:QVariantMap{{"ok",false},{"error",error}});
        };
        const auto adopt=[&](const QString &id){
            const QVariantMap arguments{{"engine","/agent/virtual-engine"},{"interactive",true},{"seconds",600}};
            const QVariant job=QVariantMap{{"id",id},{"state","running"},{"created",3000},
                {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.paravirtual"},{"arguments",arguments}}}}}};
            QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,job)));
            view->setProperty("cancelling",false);
        };
        const auto press=[&]{QVERIFY(QMetaObject::invokeMethod(stop,"clicked"));};
        const auto text=[&]{return stop->property("text").toString();};
        const auto finished=[&]{
            QVERIFY(QMetaObject::invokeMethod(view,"applyReport",Q_ARG(QVariant,QVariant(QVariantMap{{"job_state","cancelled"},{"report",QVariantMap{}}}))));
        };
        // Nothing below lets the event loop run: the real control plane never answers before the scenario does.
        // A guest that is still starting has nothing to save. Its control stop is refused, and Beenden then
        // cancels the job at once instead of leaving a button that cannot end it.
        adopt("fixture-stop-starting");
        QVERIFY(view->property("paravirtualMode").toBool());QVERIFY(stop->isEnabled());QCOMPARE(text(),QString("Stop"));
        press();
        QVERIFY(view->property("cancelling").toBool());
        auto controls=sent("ios.emulator.control","fixture-stop-starting");
        QCOMPARE(controls.size(),1);QVERIFY(sent("job.cancel","fixture-stop-starting").isEmpty());
        answer(controls[0],"ios.emulator.control",{{"code","ios_control_starting"},{"message","The guest has not published its control monitor yet"}});
        auto cancels=sent("job.cancel","fixture-stop-starting");
        QCOMPARE(cancels.size(),1);
        QVERIFY(view->property("cancelling").toBool());QVERIFY(!stop->isEnabled());QCOMPARE(text(),QString("Stopping…"));
        answer(cancels[0],"job.cancel");
        // A guest that is busy, not idle, silent or unreachable is not cancelled behind the user's back. The
        // refusal is shown and the same button ends the guest without saving on a second, deliberate click.
        const QList<QPair<QString,QString>> refusals{
            {"ios_guest_busy","A native framework request is still in progress; the emulator was not stopped"},
            {"ios_guest_not_idle","The native worker has not confirmed it is idle; the emulator was not stopped"},
            {"ios_guest_timeout","The native guest did not reply before the request deadline"},
            {"control_plane_error","The control program ended unexpectedly"},
            {"","Too many concurrent requests"}};
        for(const auto &[code,message]:refusals) {
            const QString job="fixture-stop-"+(code.isEmpty()?QString("without-code"):code);
            adopt(job);
            // A new job starts with the safe button again.
            QCOMPARE(text(),QString("Stop"));
            press();
            controls=sent("ios.emulator.control",job);QCOMPARE(controls.size(),1);
            QVariantMap error{{"message",message}};if(!code.isEmpty())error["code"]=code;
            answer(controls[0],"ios.emulator.control",error);
            QVERIFY2(sent("job.cancel",job).isEmpty(),qPrintable(code));
            QVERIFY2(!view->property("cancelling").toBool(),qPrintable(code));
            QVERIFY(view->property("failed").toBool());QVERIFY(view->property("notice").toString().contains(message));
            QVERIFY2(stop->isEnabled(),qPrintable(code));QCOMPARE(text(),QString("Stop without saving"));
            press();
            cancels=sent("job.cancel",job);QCOMPARE(cancels.size(),1);
            QVERIFY(view->property("cancelling").toBool());QCOMPARE(text(),QString("Stopping…"));
            // A cancellation the control plane refuses leaves the explicit button in place for another try.
            answer(cancels[0],"job.cancel",{{"code","job_not_found"},{"message","No such job"}});
            QVERIFY(!view->property("cancelling").toBool());QVERIFY(stop->isEnabled());QCOMPARE(text(),QString("Stop without saving"));
            press();
            cancels=sent("job.cancel",job);QCOMPARE(cancels.size(),1);
            answer(cancels[0],"job.cancel");
        }
        // A stop the guest accepts stays the safe path: it saves first and is never turned into a cancellation.
        adopt("fixture-stop-saved");press();
        controls=sent("ios.emulator.control","fixture-stop-saved");QCOMPARE(controls.size(),1);
        answer(controls[0],"ios.emulator.control");
        QVERIFY(sent("job.cancel","fixture-stop-saved").isEmpty());QVERIFY(view->property("cancelling").toBool());
        QVERIFY(view->property("notice").toString().contains("Closing the session"));QCOMPARE(text(),QString("Stopping…"));
        // Once the job has ended, the button means "Beenden" again.
        adopt("fixture-stop-ended");press();
        controls=sent("ios.emulator.control","fixture-stop-ended");QCOMPARE(controls.size(),1);
        answer(controls[0],"ios.emulator.control",{{"code","ios_guest_busy"},{"message","busy"}});
        QCOMPARE(text(),QString("Stop without saving"));
        finished();
        QVERIFY(!view->property("cancelling").toBool());QCOMPARE(text(),QString("Stop"));
        // The real control plane rejects the fixture job ids; its late answers find nothing waiting for them.
        QTest::qWait(300);
        view->setProperty("polling",false);view->setProperty("jobId","");view->setProperty("jobState","");
        view->setProperty("mode","ios");view->setProperty("interactiveGuest",false);view->setProperty("failed",false);view->setProperty("notice","");
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void ios_dialog_lists_and_changes_the_phones_apps() {
        // The profile's own apps are deployed through ios.virtual.configure. The dialog lists them from the
        // status with what the control plane knows (inspected when added, installed on this run's volume,
        // opened by the start), adds one from a path, removes one, chooses the app the start opens, and
        // shows the start's time-zone step.
        open("ios-research");
        auto *view=object("iosResearch");QVERIFY(view);
        auto *dialog=object("iosResearchDialog");QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        auto *section=item("virtualPhoneApps");QVERIFY(section);
        auto *empty=item("virtualPhoneAppsEmpty");QVERIFY(empty);
        auto *picker=item("virtualPhoneOpenApp");QVERIFY(picker);
        auto *add=item("virtualPhoneAppAdd");QVERIFY(add);
        auto *refusal=item("virtualPhoneAppError");QVERIFY(refusal);
        auto *zone=item("virtualPhoneTimeZone");QVERIFY(zone);
        const QString sampleId="io.github.marselnenaj.tomacco.sample-app",research="io.github.marselnenaj.tomacco.research-app";
        const QVariantMap sample{{"bundle","/fixture/sample-app/TomaccoSample.app"},{"bundle_id",sampleId},{"name","TomaccoSample.app"},{"display_name","Tomacco Sample"}};
        const QVariantMap byHand{{"bundle","/fixture/Notes.app"}};
        const QVariantMap notes{{"bundle","/fixture/Notes.app"},{"bundle_id","io.example.notes"},{"name","Notes.app"},{"display_name","Notes"}};
        // Every request the view made for an operation, oldest first, with its arguments.
        const auto sent=[&](const QString &operation){
            QList<QPair<int,QVariantMap>> found;const auto all=mapOf(view->property("requests"));
            for(auto it=all.begin();it!=all.end();++it) {
                const auto context=it.value().toMap();
                if(context.value("operation").toString()==operation)found.append({it.key().toInt(),context.value("arguments").toMap()});
            }
            std::sort(found.begin(),found.end(),[](const auto &a,const auto &b){return a.first<b.first;});
            return found;
        };
        const auto status=[&](const QVariantList &apps,const QString &application,const QVariantMap &installed,const QVariant &timeZone){
            return QVariantMap{{"configured",true},{"run",324},{"phase","failed"},{"guest_running",true},
                {"application",application},{"applications",apps},{"installed_applications",installed},{"time_zone",timeZone}};
        };
        const auto phone=[&](const QVariantList &apps,const QString &application,const QVariantMap &installed,const QVariant &timeZone){
            view->setProperty("virtualPhone",status(apps,application,installed,timeZone));
        };
        const auto text=[&](const char *name){auto *label=item(name);return label?label->property("text").toString():QString();};
        const auto accept=[&](int id,const QVariantMap &result){control->apiFinished(id,"ios.virtual.configure",QVariantMap{{"ok",true},{"result",result}});};
        // Nothing below lets the event loop run: the real control plane (which has no profile here) never
        // answers before the scenario does, and the status poll cannot replace the fixture state.
        phone({sample,byHand},sampleId,QVariantMap{},QVariantMap{{"zone","Europe/Vienna"},{"outcome","guest_operation_busy"}});
        QVERIFY(section->isVisible());QVERIFY(!empty->isVisible());
        QVERIFY(item("virtualPhoneApp-0"));QVERIFY(item("virtualPhoneApp-1"));QVERIFY(!item("virtualPhoneApp-2"));
        QCOMPARE(text("virtualPhoneAppTitle-0"),QString("Tomacco Sample · ")+sampleId);
        // installd refused the staged app, so it reads as inspected, not installed, and still the app to open.
        QVERIFY(text("virtualPhoneAppState-0").contains("verified"));
        QVERIFY(text("virtualPhoneAppState-0").contains("not installed"));
        QVERIFY(text("virtualPhoneAppState-0").contains("opened at start"));
        // A row written by hand names only the path: shown by its directory, as not inspected, never as chosen.
        QCOMPARE(text("virtualPhoneAppTitle-1"),QString("Notes.app"));
        QVERIFY(text("virtualPhoneAppState-1").contains("unverified"));
        QVERIFY(!text("virtualPhoneAppState-1").contains("opened"));
        QCOMPARE(picker->property("count").toInt(),2);QCOMPARE(picker->property("currentIndex").toInt(),1);
        QVERIFY(picker->property("currentText").toString().contains("Tomacco Sample"));
        // The time-zone step: a busy worker leaves the zone alone; an applied zone names the link.
        QVERIFY(zone->isVisible());
        QVERIFY(text("virtualPhoneTimeZone").contains("Europe/Vienna"));QVERIFY(text("virtualPhoneTimeZone").contains("not applied"));
        QVERIFY(text("virtualPhoneTimeZone").contains("guest_operation_busy"));
        phone({sample},sampleId,QVariantMap{{sampleId,QString(40,'0')}},QVariantMap{{"zone","Europe/Vienna"},{"outcome","applied"},
            {"previous_link","/usr/share/zoneinfo/America/Los_Angeles"},{"link","/usr/share/zoneinfo/Europe/Vienna"},{"changed",true}});
        QVERIFY(text("virtualPhoneAppState-0").contains("· installed in the current run"));
        QCOMPARE(text("virtualPhoneTimeZone"),QString("Time zone Europe/Vienna applied · /usr/share/zoneinfo/Europe/Vienna"));
        // An applied step whose link already pointed at the zone says so instead of claiming a change.
        phone({sample},sampleId,QVariantMap{},QVariantMap{{"zone","Europe/Vienna"},{"outcome","applied"},
            {"previous_link","/usr/share/zoneinfo/Europe/Vienna"},{"link","/usr/share/zoneinfo/Europe/Vienna"},{"changed",false}});
        QCOMPARE(text("virtualPhoneTimeZone"),QString("Time zone Europe/Vienna was already set · /usr/share/zoneinfo/Europe/Vienna"));
        // A skipped step shows why: the two reasons the start writes (guest tools before 368; a host zone that
        // is no zoneinfo name, recorded with zone null) are spelled out, a reason the dialog does not know is
        // shown as recorded, and a record without one says only that the step was skipped.
        phone({sample},sampleId,QVariantMap{},QVariantMap{{"zone","Europe/Vienna"},{"outcome","skipped"},{"reason","unsupported"}});
        QCOMPARE(text("virtualPhoneTimeZone"),QString("Time zone not set · the device's helper tools are too old for this (before build 368)"));
        phone({sample},sampleId,QVariantMap{},QVariantMap{{"zone",QVariant()},{"outcome","skipped"},{"reason","host_zone_unknown"}});
        QCOMPARE(text("virtualPhoneTimeZone"),QString("Time zone not set · this computer's time zone has no standard name such as Europe/Vienna"));
        phone({sample},sampleId,QVariantMap{},QVariantMap{{"zone","Europe/Vienna"},{"outcome","skipped"},{"reason","guest_tools_absent"}});
        QCOMPARE(text("virtualPhoneTimeZone"),QString("Time zone not set · guest_tools_absent"));
        phone({sample},sampleId,QVariantMap{},QVariantMap{{"zone","Europe/Vienna"},{"outcome","skipped"}});
        QCOMPARE(text("virtualPhoneTimeZone"),QString("Time zone not set · skipped"));
        // A profile without apps, and a state from before the time-zone step: the hint, no rows, no zone line.
        phone({},research,QVariantMap{},QVariant());
        QVERIFY(!zone->isVisible());QVERIFY(empty->isVisible());QVERIFY(!item("virtualPhoneApp-0"));
        QCOMPARE(picker->property("count").toInt(),1);QCOMPARE(picker->property("currentIndex").toInt(),0);
        // Adding: the path goes to ios.virtual.configure as add_application; a refusal is shown next to the
        // list with its reason and remedy, the path stays for correction and the dialog's own notice is not touched.
        QVERIFY(!add->isEnabled());
        view->setProperty("virtualAppPath","/fixture/Broken.app");
        QVERIFY(add->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(add,"clicked"));
        auto configures=sent("ios.virtual.configure");
        QCOMPARE(configures.size(),1);
        QCOMPARE(configures.last().second.value("add_application").toString(),QString("/fixture/Broken.app"));
        QVERIFY(view->property("virtualAppBusy").toBool());QVERIFY(!add->isEnabled());QVERIFY(!picker->isEnabled());
        control->apiFinished(configures.last().first,"ios.virtual.configure",QVariantMap{{"ok",false},{"error",QVariantMap{
            {"code","invalid_application_bundle"},{"message","Broken.app/Broken is built for x86_64, not arm64"},{"remedy","thin it to the arm64 slice"}}}});
        QVERIFY(!view->property("virtualAppBusy").toBool());QVERIFY(add->isEnabled());
        QVERIFY(refusal->isVisible());
        QVERIFY(refusal->property("text").toString().contains("built for x86_64, not arm64"));
        QVERIFY(refusal->property("text").toString().contains("thin it to the arm64 slice"));
        QCOMPARE(view->property("virtualAppPath").toString(),QString("/fixture/Broken.app"));
        QVERIFY(!view->property("failed").toBool());QVERIFY(view->property("notice").toString().isEmpty());
        // An accepted add comes back as the new status: the list grows, the path field clears, the refusal goes.
        view->setProperty("virtualAppPath","/fixture/Notes.app");
        QVERIFY(QMetaObject::invokeMethod(add,"clicked"));
        configures=sent("ios.virtual.configure");QCOMPARE(configures.size(),1);
        QCOMPARE(configures.last().second.value("add_application").toString(),QString("/fixture/Notes.app"));
        accept(configures.last().first,status({notes},research,QVariantMap{},QVariant()));
        QVERIFY(!refusal->isVisible());QCOMPARE(view->property("virtualAppPath").toString(),QString());
        QCOMPARE(text("virtualPhoneAppTitle-0"),QString("Notes · io.example.notes"));
        QVERIFY(!empty->isVisible());
        QCOMPARE(picker->property("count").toInt(),2);QCOMPARE(picker->property("currentIndex").toInt(),0);
        // Choosing the app the start opens is a configure of its own; the choice follows the answer.
        QVERIFY(QMetaObject::invokeMethod(picker,"activated",Q_ARG(int,1)));
        configures=sent("ios.virtual.configure");QCOMPARE(configures.size(),1);
        QCOMPARE(configures.last().second.value("application").toString(),QString("io.example.notes"));
        QVERIFY(!configures.last().second.contains("add_application"));
        accept(configures.last().first,status({notes},"io.example.notes",QVariantMap{},QVariant()));
        QCOMPARE(picker->property("currentIndex").toInt(),1);
        QVERIFY(text("virtualPhoneAppState-0").contains("opened at start"));
        // Removing names the bundle's path.
        auto *remove=item("virtualPhoneAppRemove-0");QVERIFY(remove);
        QVERIFY(QMetaObject::invokeMethod(remove,"clicked"));
        configures=sent("ios.virtual.configure");QCOMPARE(configures.size(),1);
        QCOMPARE(configures.last().second.value("remove_application").toString(),QString("/fixture/Notes.app"));
        accept(configures.last().first,status({},research,QVariantMap{},QVariant()));
        QVERIFY(empty->isVisible());QVERIFY(!item("virtualPhoneApp-0"));
        // The section and its controls fit the dialog at both window sizes.
        auto *scroll=qobject_cast<QQuickItem *>(object("iosResearchScroll"));QVERIFY(scroll);
        auto *flick=scroll->property("contentItem").value<QObject *>();QVERIFY(flick);
        for(const auto &size:{QSize(960,680),QSize(1380,970)}) {
            // A status answer arriving meanwhile would replace the phone state and hide the section; try again then.
            for(int attempt=0;;++attempt) {
                phone({sample,byHand},sampleId,QVariantMap{},QVariantMap{{"zone","Europe/Vienna"},{"outcome","guest_operation_busy"}});
                window->resize(size);QTest::qWait(150);
                flick->setProperty("contentY",qMax(0.0,flick->property("contentHeight").toReal()-scroll->height()));
                QTest::qWait(50);
                if(mapOf(view->property("virtualPhone")).value("configured").toBool())break;
                QVERIFY2(attempt<5,"the virtual iPhone state keeps being replaced");
            }
            for(auto *widget:{section,add,picker}) {
                const auto bounds=widget->mapRectToScene(widget->boundingRect());
                QVERIFY2(bounds.left()>=0 && bounds.right()<=window->width() && bounds.width()>0,
                    qPrintable(QString("%1 spans %2..%3 in a %4-wide window").arg(widget->objectName()).arg(bounds.left()).arg(bounds.right()).arg(size.width())));
            }
            capture("ios-virtual-apps-"+QString::number(size.width()));
            flick->setProperty("contentY",0);
        }
        // An .app dropped on the device's picture is added the same way, and deployed at once while the phone runs.
        auto *guest=object("iosGuestView");QVERIFY(guest);
        phone({},research,QVariantMap{},QVariant());
        QCOMPARE(guest->property("appKinds").toStringList(),QStringList{"app"});
        QVERIFY(QMetaObject::invokeMethod(view,"dropApplication",Q_ARG(QVariant,QString("/fixture/Dropped.app"))));
        configures=sent("ios.virtual.configure");QCOMPARE(configures.size(),1);
        QCOMPARE(configures.last().second.value("add_application").toString(),QString("/fixture/Dropped.app"));
        QVERIFY(configures.last().second.value("deploy").toBool());
        QCOMPARE(guest->property("appNotice").toString(),QString("Checking Dropped.app…"));
        accept(configures.last().first,status({},research,QVariantMap{},QVariant()));
        QCOMPARE(guest->property("appNotice").toString(),QString("Dropped.app added · installing on the running device"));
        guest->setProperty("appNotice",QString());
        // Leave nothing behind; the real control plane's late refusals of the fixture paths find nothing waiting.
        view->setProperty("virtualAppPath",QString());view->setProperty("virtualAppNotice",QString());view->setProperty("virtualAppBusy",false);
        view->setProperty("virtualPhone",QVariantMap{{"configured",false}});
        QTest::qWait(300);
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void pending_guest_detach_is_reported_expires_and_does_not_keep_polling() {
        // The Apple page is not on screen: only a pending request or a detached guest may keep the status poll.
        QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
        auto *view=object("iosResearch");QVERIFY(view);
        auto *poll=object("virtualStatusPoll");QVERIFY(poll);
        auto *guestView=object("iosGuestView");QVERIFY(guestView);
        // No report poll may change a guest job that this scenario adopts by hand.
        view->setProperty("polling",true);
        const auto state=[&]{QVariant value;QMetaObject::invokeMethod(view,"guestState",Q_RETURN_ARG(QVariant,value));return mapOf(value);};
        const auto detach=[&](bool detached){QVERIFY(QMetaObject::invokeMethod(view,"configureGuest",Q_ARG(QVariant,QVariant(QVariantMap{{"detached",detached}}))));};
        const auto phone=[&](bool configured){view->setProperty("virtualPhone",QVariantMap{{"configured",configured}});};
        const auto polling=[&]{return poll->property("running").toBool();};
        const auto start=[&](const QString &id){
            const QVariantMap arguments{{"engine","/agent/virtual-engine"},{"interactive",true},{"seconds",600}};
            const QVariant job=QVariantMap{{"id",id},{"state","running"},{"created",4000},
                {"plan",QVariantMap{{"operation_key",QVariantMap{{"operation","ios.emulator.paravirtual"},{"arguments",arguments}}}}}};
            QVERIFY(QMetaObject::invokeMethod(view,"adoptJob",Q_ARG(QVariant,job)));
        };
        const auto end=[&]{QVERIFY(QMetaObject::invokeMethod(view,"applyReport",Q_ARG(QVariant,QVariant(QVariantMap{{"job_state","cancelled"},{"report",QVariantMap{}}}))));};
        // Answers to the status call that a request makes would replace the phone state set here: they are
        // not awaited before the assertions that depend on it.
        phone(false);
        QVERIFY(state().contains("detach_pending"));QVERIFY(!state().value("detach_pending").toBool());
        QVERIFY(!polling());
        // A request for a guest that does not exist is kept and reported. With no virtual iPhone to follow,
        // it needs no status poll.
        detach(true);
        QVERIFY(state().value("detach_pending").toBool());QVERIFY(!polling());
        // A configured virtual iPhone is followed through its status while the request waits.
        phone(true);
        QVERIFY(polling());
        // When the guest appears the window opens at once, and the request is no longer pending.
        start("fixture-detach-pending");
        QVERIFY(guestView->property("running").toBool());QVERIFY(guestView->property("detached").toBool());
        QVERIFY(!state().value("detach_pending").toBool());QVERIFY(state().value("detached").toBool());
        // Docking the window ends the request.
        guestView->setProperty("detached",false);
        QVERIFY(!view->property("wantDetached").toBool());QVERIFY(!state().value("detach_pending").toBool());
        end();
        QVERIFY(!guestView->property("running").toBool());
        // A request that finds no guest is dropped after its time-box, and a guest that starts later is not
        // opened in a window nobody asked for any more.
        QVERIFY(view->property("detachTimeoutMs").isValid());
        view->setProperty("detachTimeoutMs",150);
        detach(true);
        QVERIFY(state().value("detach_pending").toBool());
        QTRY_VERIFY_WITH_TIMEOUT(!state().value("detach_pending").toBool(),patient(3000));
        QVERIFY(!view->property("wantDetached").toBool());
        phone(true);QVERIFY(!polling());
        start("fixture-detach-late");
        QVERIFY(guestView->property("running").toBool());QVERIFY(!guestView->property("detached").toBool());
        end();
        view->setProperty("detachTimeoutMs",600000);view->setProperty("polling",false);
        view->setProperty("jobId","");view->setProperty("jobState","");view->setProperty("mode","ios");view->setProperty("interactiveGuest",false);
        view->setProperty("virtualPhone",QVariantMap{{"configured",false}});
        QTest::qWait(250);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void awaiting_a_device_never_starts_audio_for_another_session() {
        QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
        auto *view=object("nativeWorkspace");QVERIFY(view);
        const auto map=[&](const char *name){return mapOf(view->property(name));};
        const auto reply=[&](int id,const QString &op,const QVariant &result){
            auto requests=map("requests");
            requests[QString::number(id)]=QVariantMap{{"op",op},{"quiet",true}};
            view->setProperty("requests",requests);
            control->apiFinished(id,op,QVariantMap{{"ok",true},{"result",result}});
        };
        // Every request the view made, by operation.
        const auto sent=[&](const QString &op){
            QVariantList found;const auto all=map("requests");
            for(const auto &entry:all)if(entry.toMap().value("op").toString()==op)found.append(entry);
            return found;
        };
        const auto platform=view->property("platform"),target=view->property("target"),autoAttach=view->property("autoAttach");
        // The app shows Apple devices while an Android emulator is connected elsewhere, and a second Android
        // device is awaited. The connected emulator is not the one the user asked for.
        view->setProperty("requests",QVariantMap{});view->setProperty("live",QVariantMap{});
        view->setProperty("sessions",QVariantList{});view->setProperty("targets",QVariantList{});
        view->setProperty("platform","ios");view->setProperty("forwardAudio",true);
        const auto device=[](const QString &id){return QVariantMap{{"id",id},{"name",id},{"avd",id},{"platform","android"},{"kind","emulator"},{"state","device"},{"booted",true}};};
        const QVariantMap foreign{{"session","audio-foreign-session"},{"platform","android"},{"status","connected"},{"device","emulator-foreign"}};
        QVERIFY(QMetaObject::invokeMethod(view,"configure",Q_ARG(QVariant,QVariant(QVariantMap{{"device","emulator-await"},{"connect",true}}))));
        const bool waiting=view->property("pending").toBool();
        // Nothing below lets the event loop run, so no real answer can interleave with the ones given here.
        reply(-81,"runtime.list",QVariantList{foreign});
        reply(-82,"device.list",QVariantMap{{"devices",QVariantList{device("emulator-await"),device("emulator-foreign")}}});
        const auto audio=sent("runtime.audio"),starts=sent("runtime.start");
        const auto shownPlatform=view->property("platform").toString(),shownTarget=view->property("target").toString();
        const auto liveSession=map("live").value("session").toString(),audioPending=view->property("audioPendingSession").toString();
        // Leave nothing behind for the scenarios that follow.
        QVERIFY(QMetaObject::invokeMethod(view,"clearPending"));
        view->setProperty("connecting",false);view->setProperty("requests",QVariantMap{});view->setProperty("audioPendingSession",QString());
        view->setProperty("sessions",QVariantList{});view->setProperty("targets",QVariantList{});view->setProperty("live",QVariantMap{});
        view->setProperty("platform",platform);view->setProperty("target",target);view->setProperty("autoAttach",autoAttach);
        view->setProperty("notice",QString());view->setProperty("failed",false);
        QTest::qWait(300);
        QVERIFY(waiting);
        QVERIFY2(audio.isEmpty(),"audio forwarding was started for a session that is not the awaited device");
        QVERIFY(audioPending.isEmpty());QVERIFY(liveSession.isEmpty());
        // The awaited device itself is connected, with the app's own audio setting.
        QCOMPARE(shownPlatform,QString("android"));QCOMPARE(shownTarget,QString("emulator-await"));
        QCOMPARE(starts.size(),1);QVERIFY(starts[0].toMap().value("signature").toString().contains("emulator-await"));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void apps_dropped_on_the_device_install_there() {
        // Files dragged onto the device view: APKs install on an Android device, one IPA on an iPhone, through
        // device.install. Other app files are explained; files that are no app pass on.
        open("runtime");
        window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *area=item("nativeAppDrop");QVERIFY(area);
        auto *overlay=visualItem(area,"appDropOverlay");QVERIFY(overlay);
        const auto platform=view->property("platform"),target=view->property("target"),appleTarget=view->property("appleTarget");
        view->setProperty("requests",QVariantMap{});view->setProperty("live",QVariantMap{});
        view->setProperty("targets",QVariantList{
            QVariantMap{{"id","drop-pixel"},{"name","Drop Pixel"},{"platform","android"},{"kind","device"},{"state","device"}},
            QVariantMap{{"id","drop-iphone"},{"name","Drop iPhone"},{"platform","ios"},{"kind","device"},{"state","connected"}}});
        view->setProperty("platform","android");view->setProperty("target","drop-pixel");
        QTemporaryDir files;QVERIFY(files.isValid());
        for(const auto *name:{"base.apk","split.apk","App.ipa","notes.txt"}){QFile file(files.filePath(name));QVERIFY(file.open(QIODevice::WriteOnly));}
        const auto url=[&](const char *name){return QUrl::fromLocalFile(files.filePath(name));};
        const QPoint at=area->mapToScene(QPointF(area->width()/2,area->height()/2)).toPoint();
        QMimeData data;
        // Enters with these files and reports whether the view took the drag.
        const auto enter=[&](const QList<QUrl> &urls){
            data.setUrls(urls);
            QDragEnterEvent event(at,Qt::CopyAction,&data,Qt::LeftButton,Qt::NoModifier);
            QCoreApplication::sendEvent(window,&event);
            return event.isAccepted();
        };
        const auto drop=[&]{QDropEvent event(at,Qt::CopyAction,&data,Qt::LeftButton,Qt::NoModifier);QCoreApplication::sendEvent(window,&event);};
        const auto leave=[&]{QDragLeaveEvent event;QCoreApplication::sendEvent(window,&event);};
        const auto installs=[&]{
            QStringList found;const auto all=mapOf(view->property("requests"));
            for(const auto &entry:all)if(entry.toMap().value("op").toString()=="device.install")found.append(entry.toMap().value("signature").toString());
            return found;
        };
        // Split APKs install together on the Android device.
        QVERIFY(enter({url("base.apk"),url("split.apk")}));
        QVERIFY(overlay->isVisible());QVERIFY(area->property("fits").toBool());
        drop();
        QVERIFY(!overlay->isVisible());
        auto sent=installs();QCOMPARE(sent.size(),1);
        auto arguments=QJsonDocument::fromJson(sent[0].toUtf8()).toVariant().toMap();
        QCOMPARE(arguments.value("platform").toString(),QString("android"));QCOMPARE(arguments.value("device").toString(),QString("drop-pixel"));
        QCOMPARE(arguments.value("paths").toStringList(),(QStringList{files.filePath("base.apk"),files.filePath("split.apk")}));
        QVERIFY(view->property("notice").toString().startsWith("Installing base.apk, split.apk on "));QVERIFY(!view->property("failed").toBool());
        // An IPA is no Android app: said so, nothing is sent.
        view->setProperty("requests",QVariantMap{});
        QVERIFY(enter({url("App.ipa")}));QVERIFY(!area->property("fits").toBool());
        drop();
        QVERIFY(installs().isEmpty());
        QCOMPARE(view->property("notice").toString(),QString("Android devices install .apk files."));QVERIFY(view->property("failed").toBool());
        // A file that is no app is not taken here.
        QVERIFY(!enter({url("notes.txt")}));QVERIFY(!overlay->isVisible());leave();
        // An iPhone over USB takes one signed IPA.
        view->setProperty("platform","ios");view->setProperty("appleTarget","device");view->setProperty("target","drop-iphone");
        QVERIFY(enter({url("App.ipa")}));QVERIFY(area->property("fits").toBool());
        drop();
        sent=installs();QCOMPARE(sent.size(),1);
        arguments=QJsonDocument::fromJson(sent[0].toUtf8()).toVariant().toMap();
        QCOMPARE(arguments.value("platform").toString(),QString("ios"));QCOMPARE(arguments.value("device").toString(),QString("drop-iphone"));
        QCOMPARE(arguments.value("paths").toStringList(),QStringList{files.filePath("App.ipa")});
        QVERIFY(enter({url("base.apk")}));QVERIFY(!area->property("fits").toBool());leave();
        // Leave nothing behind for the scenarios that follow.
        view->setProperty("requests",QVariantMap{});view->setProperty("targets",QVariantList{});
        view->setProperty("platform",platform);view->setProperty("appleTarget",appleTarget);view->setProperty("target",target);
        view->setProperty("notice",QString());view->setProperty("failed",false);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void screenshots_are_saved_and_copied() {
        // A capture of the live device is saved by the control plane and then put on the clipboard;
        // without a device the reason is shown. The palette and the shortcut sheet name it.
        open("runtime");
        auto *view=object("nativeWorkspace");QVERIFY(view);
        view->setProperty("live",QVariantMap{});view->setProperty("requests",QVariantMap{});
        QVERIFY(QMetaObject::invokeMethod(view,"capture"));
        QCOMPARE(view->property("notice").toString(),QString("Connect a device to take a screenshot."));
        QVERIFY(view->property("failed").toBool());
        QTemporaryDir pictures;QVERIFY(pictures.isValid());
        const auto path=pictures.filePath("shot.png");
        QImage picture(36,64,QImage::Format_RGB32);picture.fill(QColor("#2fa84f"));QVERIFY(picture.save(path));
        QGuiApplication::clipboard()->clear();
        view->setProperty("requests",QVariantMap{{"-61",QVariantMap{{"op","runtime.capture"}}}});
        control->apiFinished(-61,"runtime.capture",QVariantMap{{"ok",true},{"result",QVariantMap{{"path",path}}}});
        QCOMPARE(view->property("notice").toString(),QString("Screenshot saved and copied · ")+path);
        QVERIFY(!view->property("failed").toBool());
        const auto copied=QGuiApplication::clipboard()->image();
        QCOMPARE(copied.size(),picture.size());QCOMPARE(copied.pixelColor(10,10),QColor("#2fa84f"));
        // A file that cannot be read is still reported as saved, but not as copied.
        view->setProperty("requests",QVariantMap{{"-62",QVariantMap{{"op","runtime.capture"}}}});
        control->apiFinished(-62,"runtime.capture",QVariantMap{{"ok",true},{"result",QVariantMap{{"path",pictures.filePath("missing.png")}}}});
        QCOMPARE(view->property("notice").toString(),QString("Capture saved · ")+pictures.filePath("missing.png"));
        bool listed=false;
        for(const auto &command:view->property("commands").value<QJSValue>().toVariant().toList())
            if(command.toMap().value("id").toString()=="screenshot")listed=command.toMap().value("key").toString()=="Ctrl+Shift+S";
        QVERIFY(listed);
        view->setProperty("notice",QString());view->setProperty("failed",false);view->setProperty("requests",QVariantMap{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void recent_projects_reopen_from_the_start_screen_and_the_palette() {
        // Every project opened is remembered, newest first, without duplicates or folders that are gone;
        // the start screen and the palette offer the others again.
        open("runtime");
        window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        const auto original=directory.filePath("project");
        QTRY_VERIFY(backend->recentProjects().contains(QFileInfo(original).canonicalFilePath()));
        QTemporaryDir other;QVERIFY(other.isValid());
        const auto first=other.filePath("first"),second=other.filePath("second"),gone=other.filePath("gone");
        for(const auto &folder:{first,second,gone})QVERIFY(QDir().mkpath(folder));
        QFile pubspec(second+"/pubspec.yaml");QVERIFY(pubspec.open(QIODevice::WriteOnly));pubspec.write("name: second_app\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n");pubspec.close();
        QSignalSpy changed(backend.get(),&Backend::recentProjectsChanged);
        backend->rememberProject(first);backend->rememberProject(gone);backend->rememberProject(second);backend->rememberProject(first);
        QCOMPARE(changed.size(),4);
        QVERIFY(QDir(gone).removeRecursively());
        auto recent=backend->recentProjects();
        QCOMPARE(recent.mid(0,2),(QStringList{QFileInfo(first).canonicalFilePath(),QFileInfo(second).canonicalFilePath()}));
        QVERIFY(!recent.contains(gone));QCOMPARE(recent.count(QFileInfo(first).canonicalFilePath()),1);
        backend->rememberProject(first);QCOMPARE(changed.size(),4);
        backend->rememberProject(other.filePath("missing"));QCOMPARE(changed.size(),4);
        // The start screen names the newest ones; the open project is not among them.
        QTRY_VERIFY(item("heroRecent-0") && item("heroRecent-1"));
        auto *button=item("heroRecent-0");QVERIFY(button->isVisible());
        QCOMPARE(button->property("text").toString(),QString("first"));
        QCOMPARE(item("heroRecent-1")->property("text").toString(),QString("second"));
        for(int index=0;index<3;++index)
            if(auto *shown=item(qPrintable(QString("heroRecent-%1").arg(index))))QVERIFY(shown->property("tip").toString()!=original);
        // The palette finds them by name and opens one.
        auto *palette=object("commandPalette");QVERIFY(palette);
        QVERIFY(QMetaObject::invokeMethod(palette,"open"));
        QTRY_VERIFY(item("commandSearch") && item("commandSearch")->hasActiveFocus());
        type("second");
        QTRY_COMPARE(palette->property("results").value<QJSValue>().property(0).property("title").toString(),QString("Open recent project second"));
        QTest::keyClick(window,Qt::Key_Return);
        const auto path=[&]{return mapOf(workspace->property("project")).value("path").toString();};
        QTRY_COMPARE_WITH_TIMEOUT(QFileInfo(path()).canonicalFilePath(),QFileInfo(second).canonicalFilePath(),patient(10000));
        QCOMPARE(backend->recentProjects().first(),QFileInfo(second).canonicalFilePath());
        // Back to the project the other scenarios use, and only it is remembered from here.
        QVERIFY(QMetaObject::invokeMethod(workspace,"openProject",Q_ARG(QVariant,original)));
        QTRY_COMPARE_WITH_TIMEOUT(path(),original,patient(10000));
        for(const auto &folder:backend->recentProjects())if(folder!=QFileInfo(original).canonicalFilePath())backend->forgetProject(folder);
        QTRY_VERIFY(!item("heroRecent-0") || !item("heroRecent-0")->isVisible());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void a_project_folder_dropped_on_the_window_opens() {
        // A folder dragged onto the window opens as the project and shows its overview; a file does not.
        open("runtime");
        window->resize(1380,970);QTest::qWait(50);
        auto *overlay=item("projectFolderDropOverlay");QVERIFY(overlay);
        const auto original=directory.filePath("project");
        QTemporaryDir other;QVERIFY(other.isValid());
        const auto folder=other.filePath("dropped");QVERIFY(QDir().mkpath(folder));
        QFile pubspec(folder+"/pubspec.yaml");QVERIFY(pubspec.open(QIODevice::WriteOnly));
        pubspec.write("name: dropped_app\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter\n");pubspec.close();
        const QPoint at(window->width()/2,window->height()/2);
        QMimeData data;
        const auto enter=[&](const QString &path){
            data.setUrls({QUrl::fromLocalFile(path)});
            QDragEnterEvent event(at,Qt::CopyAction,&data,Qt::LeftButton,Qt::NoModifier);
            QCoreApplication::sendEvent(window,&event);
            return event.isAccepted();
        };
        QVERIFY(!enter(folder+"/pubspec.yaml"));QVERIFY(!overlay->isVisible());
        {QDragLeaveEvent event;QCoreApplication::sendEvent(window,&event);}
        QVERIFY(enter(folder));QVERIFY(overlay->isVisible());
        QVERIFY(overlay->property("text").toString().contains("dropped"));
        {QDropEvent event(at,Qt::CopyAction,&data,Qt::LeftButton,Qt::NoModifier);QCoreApplication::sendEvent(window,&event);}
        QVERIFY(!overlay->isVisible());
        const auto path=[&]{return mapOf(workspace->property("project")).value("path").toString();};
        QTRY_COMPARE_WITH_TIMEOUT(QFileInfo(path()).canonicalFilePath(),QFileInfo(folder).canonicalFilePath(),patient(10000));
        QVERIFY(workspace->property("opened").toBool());QCOMPARE(workspace->property("section").toString(),QString("project"));
        // Back to the project the other scenarios use; the dropped one is not kept among the recent ones.
        open("runtime");
        QTRY_COMPARE_WITH_TIMEOUT(path(),original,patient(10000));
        backend->forgetProject(QFileInfo(folder).canonicalFilePath());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void the_window_reopens_at_its_last_size() {
        // Closing stores the size, or that the window was maximised; the next start opens it that way.
        open("runtime");
        window->resize(700,540);QTest::qWait(50);
        QVERIFY(QMetaObject::invokeMethod(window,"saveGeometry"));
        QSettings settings;
        QCOMPARE(settings.value("window/width").toInt(),700);QCOMPARE(settings.value("window/height").toInt(),540);
        QCOMPARE(settings.value("window/maximized").toString(),QString("false"));
        window->resize(1380,970);QTest::qWait(50);
        // (No larger than the screen: the test's offscreen screen is 800×600.)
        QVERIFY(QMetaObject::invokeMethod(window,"restoreGeometry"));
        QTRY_COMPARE(window->width(),700);QTRY_COMPARE(window->height(),540);
        // A stored size below the smallest window is ignored.
        backend->saveSetting("window/width",200);backend->saveSetting("window/height",100);
        QVERIFY(QMetaObject::invokeMethod(window,"restoreGeometry"));
        QTest::qWait(50);QCOMPARE(window->width(),700);
        window->resize(1380,970);QTest::qWait(50);
        for(const auto *key:{"window/width","window/height","window/maximized"})settings.remove(key);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void german_interface_switches_live() {
        open("project");
        // The navigation is rebuilt in the new language, so its buttons are looked up each time.
        const auto label=[&](const char *name){auto *button=item(name);return button?button->property("text").toString():QString();};
        auto *picker=object("languagePicker");QVERIFY(picker);
        QCOMPARE(label("nav-files"),QString("Files"));
        QTRY_COMPARE(picker->property("currentIndex").toInt(),1);
        // English plural forms come from the plurals-only English file.
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,1),QString("1 model"));
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,3),QString("3 models"));
        // Choosing Deutsch in the settings persists and retranslates the running interface.
        QVERIFY(QMetaObject::invokeMethod(picker,"activated",Q_ARG(int,2)));
        QCOMPARE(backend->language(),QString("de"));QCOMPARE(backend->uiLanguage(),QString("de"));
        QCOMPARE(QSettings().value("language").toString(),QString("de"));
        QTRY_COMPARE(label("nav-files"),QString("Dateien"));
        QTRY_COMPARE(picker->property("currentIndex").toInt(),2);
        QCOMPARE(object("languagePicker")->property("currentText").toString(),QString("Deutsch"));
        QCOMPARE(label("nav-runtime"),QString("Live-Gerät"));
        QCOMPARE(label("nav-jobs"),QString("Builds & Aufträge"));
        // Today's German texts, including the ones C++ produces and the operation titles.
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,1),QString("1 Modell"));
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,3),QString("3 Modelle"));
        QCOMPARE(QCoreApplication::translate("IosGuestView","%n new frame(s)/s",nullptr,1),QString("1 neues Bild/s"));
        QCOMPARE(QCoreApplication::translate("IosGuestView","%n new frame(s)/s",nullptr,4),QString("4 neue Bilder/s"));
        QCOMPARE(QCoreApplication::translate("Control","Too many concurrent requests"),QString("Zu viele gleichzeitige Anfragen"));
        QCOMPARE(QCoreApplication::translate("Operations","Set up virtual iOS device"),QString("Virtuelles iOS-Gerät einrichten"));
        QVERIFY(QMetaObject::invokeMethod(workspace,"showAction",Q_ARG(QVariant,QString("doctor"))));
        QCOMPARE(workspace->property("status").toString(),QString("Bereit · Toolchain prüfen"));
        // tomaccoctl keeps the English title for agents.
        bool found=false;
        for(const auto &operation:control->operations())if(operation.toMap().value("name")=="doctor"){found=true;QCOMPARE(operation.toMap().value("title").toString(),QString("Check toolchain"));}
        QVERIFY(found);
        capture("language-de");
        // The About dialog's longer German texts still fit.
        auto *about=object("aboutDialog");QVERIFY(about);
        QMetaObject::invokeMethod(about,"open");QTRY_VERIFY(about->property("visible").toBool());
        QCOMPARE(about->property("title").toString(),QString("Über Tomacco"));
        capture("about-de");
        QMetaObject::invokeMethod(about,"close");QTRY_VERIFY(!about->property("visible").toBool());
        // TOMACCO_LANGUAGE overrides the stored choice.
        qputenv("TOMACCO_LANGUAGE","en");backend->setLanguage("de");
        QCOMPARE(backend->uiLanguage(),QString("en"));QTRY_COMPARE(label("nav-files"),QString("Files"));
        qunsetenv("TOMACCO_LANGUAGE");
        // Back to English for good, again without a restart.
        QVERIFY(QMetaObject::invokeMethod(picker,"activated",Q_ARG(int,1)));
        QCOMPARE(backend->language(),QString("en"));QCOMPARE(backend->uiLanguage(),QString("en"));
        QTRY_COMPARE(label("nav-files"),QString("Files"));
        QVERIFY(QMetaObject::invokeMethod(workspace,"showAction",Q_ARG(QVariant,QString("doctor"))));
        QCOMPARE(workspace->property("status").toString(),QString("Ready · Check toolchain"));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    // The other translated interfaces: chosen in the settings, applied at once, with their
    // plural rules (French uses the singular for 0) and the texts C++ and the operations give.
    void translated_interfaces_switch_live_data() {
        for(const auto *column:{"code","name","files","zeroModels","oneModel","threeModels","busy","ready","about"})QTest::addColumn<QString>(column);
        QTest::newRow("fr")<<"fr"<<"Français"<<"Fichiers"<<"0 modèle"<<"1 modèle"<<"3 modèles"<<"Trop de requêtes simultanées"<<"Prêt · Vérifier la chaîne d'outils"<<"À propos de Tomacco";
        QTest::newRow("it")<<"it"<<"Italiano"<<"File"<<"0 modelli"<<"1 modello"<<"3 modelli"<<"Troppe richieste simultanee"<<"Pronto · Verifica la toolchain"<<"Informazioni su Tomacco";
        QTest::newRow("es")<<"es"<<"Español"<<"Archivos"<<"0 modelos"<<"1 modelo"<<"3 modelos"<<"Demasiadas solicitudes simultáneas"<<"Listo · Comprobar la cadena de herramientas"<<"Acerca de Tomacco";
    }
    void translated_interfaces_switch_live() {
        QFETCH(QString,code);QFETCH(QString,name);QFETCH(QString,files);QFETCH(QString,zeroModels);QFETCH(QString,oneModel);QFETCH(QString,threeModels);QFETCH(QString,busy);QFETCH(QString,ready);QFETCH(QString,about);
        open("project");
        const auto label=[&](const char *button){auto *found=item(button);return found?found->property("text").toString():QString();};
        auto *picker=object("languagePicker");QVERIFY(picker);
        const auto codes=picker->property("codes").toStringList();
        const int index=codes.indexOf(code);QVERIFY(index>0);
        // English again afterwards, also when a check fails, so the next test reads English.
        const auto english=qScopeGuard([&]{backend->setLanguage("en");});
        QVERIFY(QMetaObject::invokeMethod(picker,"activated",Q_ARG(int,index)));
        QCOMPARE(backend->language(),code);QCOMPARE(backend->uiLanguage(),code);
        QCOMPARE(QSettings().value("language").toString(),code);
        QTRY_COMPARE(label("nav-files"),files);
        // The retranslated model resets the box; it selects the choice again right after.
        QTRY_COMPARE(picker->property("currentIndex").toInt(),index);
        QCOMPARE(picker->property("currentText").toString(),name);
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,0),zeroModels);
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,1),oneModel);
        QCOMPARE(QCoreApplication::translate("Main","%n model(s)",nullptr,3),threeModels);
        QCOMPARE(QCoreApplication::translate("Control","Too many concurrent requests"),busy);
        QVERIFY(QMetaObject::invokeMethod(workspace,"showAction",Q_ARG(QVariant,QString("doctor"))));
        QCOMPARE(workspace->property("status").toString(),ready);
        auto *dialog=object("aboutDialog");QVERIFY(dialog);
        QMetaObject::invokeMethod(dialog,"open");QTRY_VERIFY(dialog->property("visible").toBool());
        QCOMPARE(dialog->property("title").toString(),about);
        capture("about-"+code);
        QMetaObject::invokeMethod(dialog,"close");QTRY_VERIFY(!dialog->property("visible").toBool());
        capture("language-"+code);
        QVERIFY(QMetaObject::invokeMethod(picker,"activated",Q_ARG(int,1)));
        QTRY_COMPARE(label("nav-files"),QString("Files"));
        QTRY_COMPARE(picker->property("currentIndex").toInt(),1);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void about_dialog_names_version_license_and_source() {
        open("runtime");
        auto *button=item("aboutButton");QVERIFY(button);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,button->mapToScene(QPointF(button->width()/2,button->height()/2)).toPoint());
        auto *dialog=object("aboutDialog");QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        QCOMPARE(object("aboutVersion")->property("text").toString(),QString("Version ")+QCoreApplication::applicationVersion());
        // The control program's own version, read from its schema.
        QVERIFY(!control->cliVersion().isEmpty());QVERIFY(control->cliError().isEmpty());
        QCOMPARE(object("aboutCliVersion")->property("text").toString(),"tomaccoctl "+control->cliVersion());
        capture("about");
        // The license ships with every build; only the named legal documents can be opened.
        QVERIFY(QFileInfo(backend->documentPath("LICENSE")).isFile());
        QVERIFY(QFileInfo(backend->documentPath("RELEASE_NOTES.md")).isFile());
        QVERIFY(backend->documentPath("../LICENSE").isEmpty());QVERIFY(backend->documentPath("CMakeLists.txt").isEmpty());
        QVERIFY(backend->sourceUrl().scheme()=="https");
        QMetaObject::invokeMethod(dialog,"close");QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void appearance_follows_the_choice_at_once_and_is_stored() {
        open("runtime");
        const auto ground=[&]{return window->property("color").value<QColor>();};
        backend->setAppearance("dark");QVERIFY(backend->dark());
        const auto dark=ground();QVERIFY(dark.lightnessF()<.2);
        backend->setAppearance("light");QVERIFY(!backend->dark());
        // Bindings follow without a restart, down to the controls.
        QVERIFY(ground().lightnessF()>.8);
        QCOMPARE(QSettings().value("appearance").toString(),QString("light"));
        // TOMACCO_THEME overrides the stored choice for one run.
        qputenv("TOMACCO_THEME","dark");QVERIFY(backend->dark());qunsetenv("TOMACCO_THEME");QVERIFY(!backend->dark());
        // Nonsense counts as following the system.
        backend->setAppearance("sepia");QCOMPARE(backend->appearance(),QString("system"));
        backend->setAppearance("system");
        auto *picker=object("appearancePicker");QVERIFY(picker);QCOMPARE(picker->property("current").toString(),QString("system"));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void activity_shows_running_jobs_and_agent_calls() {
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *panel=object("activityPanel");QVERIFY(panel);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        QVERIFY(qobject_cast<QQuickItem *>(panel)->isVisible());
        // A job from any client: the panel reads what job.list returns.
        const double now=QDateTime::currentSecsSinceEpoch();
        const QVariantMap job{{"id","activity-ui-job"},{"label","Build Android · debug"},{"state","running"},{"phase","building"},{"created",now-75},{"updated",now},
            {"plan",QVariantMap{{"steps",QVariantList{QVariantMap{{"argv",QVariantList{"/usr/bin/flutter","build","apk"}}}}}}}};
        workspace->setProperty("jobs",QVariantList{job});
        auto *row=item("activityJob-activity-ui-job");QVERIFY(row);QVERIFY(row->isVisible());
        QVERIFY(!item("activityIdle")->isVisible());
        QTRY_VERIFY(item("statusJobs")->isVisible());
        // A call an agent makes to this window, repeats folded into one line.
        QVERIFY(!item("activityCall"));
        emit control->previewRequest(-90,"state",{});emit control->previewRequest(-91,"state",{});
        QCOMPARE(mapOf(panel->property("calls").value<QJSValue>().property(0).toVariant()).value("count").toInt(),2);
        QTRY_VERIFY(item("activityCall"));
        // The panel gives way in a narrow window and comes back on request.
        window->resize(900,700);QTRY_VERIFY(!qobject_cast<QQuickItem *>(panel)->isVisible());
        QTest::keyClick(window,Qt::Key_J,Qt::ControlModifier);QTRY_VERIFY(qobject_cast<QQuickItem *>(panel)->isVisible());
        QTest::keyClick(window,Qt::Key_J,Qt::ControlModifier);QTRY_VERIFY(!qobject_cast<QQuickItem *>(panel)->isVisible());
        view->setProperty("showActivity",true);
        window->resize(1380,970);
        workspace->setProperty("jobs",QVariantList{});
        panel->setProperty("calls",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void shortcuts_reach_every_view_and_can_be_looked_up() {
        const QList<std::pair<Qt::Key,QString>> keys{{Qt::Key_2,"files"},{Qt::Key_3,"jobs"},{Qt::Key_4,"devices"},{Qt::Key_5,"project"},{Qt::Key_6,"debug"},{Qt::Key_7,"testing"},{Qt::Key_8,"profiling"},{Qt::Key_9,"release"},{Qt::Key_0,"tools"}};
        for(const auto &[key,section]:keys) {QTest::keyClick(window,key,Qt::ControlModifier);QTRY_COMPARE(workspace->property("section").toString(),section);QVERIFY(workspace->property("opened").toBool());}
        QTest::keyClick(window,Qt::Key_1,Qt::ControlModifier);QTRY_VERIFY(!workspace->property("opened").toBool());
        auto *sheet=object("shortcutsDialog");QVERIFY(sheet);
        QTest::keyClick(window,Qt::Key_F1);QTRY_VERIFY(sheet->property("visible").toBool());
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!sheet->property("visible").toBool());
        // The palette reaches commands by name, and runs them.
        auto *palette=object("commandPalette");QVERIFY(palette);
        QTest::keyClick(window,Qt::Key_K,Qt::ControlModifier);QTRY_VERIFY(item("commandSearch")->hasActiveFocus());
        type("Settings");QTest::keyClick(window,Qt::Key_Return);
        auto *settings=object("settingsDialog");QVERIFY(settings);
        QTRY_VERIFY(settings->property("visible").toBool());
        // Updates: the choice is there, and with updates forced off it is not offered.
        auto *updateMode=object("updateModePicker");QVERIFY(updateMode);
        QTRY_VERIFY(!updateMode->property("enabled").toBool());
        QCOMPARE(updateMode->property("current").toString(),QString("off"));
        QVERIFY(object("updateState"));
        QVERIFY(!object("updateInstall")->property("visible").toBool());
        QVERIFY(!object("statusUpdate")->property("visible").toBool());
        QTest::keyClick(window,Qt::Key_Escape);QTRY_VERIFY(!settings->property("visible").toBool());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    // The launch screen at the sizes people use, with and without the panel beside it: the text
    // and its buttons lie inside the visible stage, and the tomato reaches past both edges at the
    // bottom, so no background shows in the lower corners.
    void launch_screen_keeps_its_text_and_fills_its_lower_corners() {
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *display=qobject_cast<NativeView *>(object("nativeDisplay"));QVERIFY(display);
        view->setProperty("autoAttach",false);view->setProperty("live",QVariantMap{});display->setProperty("session",QVariantMap{});view->setProperty("targets",QVariantList{});
        workspace->setProperty("jobs",QVariantList{});
        const auto activity=view->property("showActivity");
        for(const auto *language:{"en","de","fr","es","it"}) {
            backend->setLanguage(language);
            for(const auto &size:{QSize(1920,1080),QSize(1440,900),QSize(1280,720),QSize(1000,700),QSize(900,700),QSize(960,1040),QSize(720,1040),QSize(600,1040),QSize(560,480)})for(const bool panel:{true,false}) {
                const auto where=QString("%1x%2 %3 panel=%4").arg(size.width()).arg(size.height()).arg(language).arg(panel);
                window->resize(size);open("runtime");QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
                view->setProperty("showActivity",panel);view->setProperty("activityForced",panel);
                QTest::qWait(60);
                auto *hero=item("nativeEmptyState");QVERIFY(hero);
                if(panel && size.width()<860) {
                    // In a narrow window the panel takes the stage instead of squeezing it.
                    auto *activity=item("activityPanel");QVERIFY(activity);QVERIFY(activity->isVisible());QVERIFY(!hero->isVisible());
                    QVERIFY(activity->width()>size.width()-Theme_railPlusBorder);
                    continue;
                }
                QVERIFY(hero->isVisible());
                auto *text=item("heroText");auto *action=item("heroAction");auto *title=item("heroTitle");QVERIFY(text);QVERIFY(action);QVERIFY(title);
                const auto stage=hero->mapRectToScene(hero->boundingRect());
                for(auto *inner:{text,action,title}) {
                    const auto bounds=inner->mapRectToScene(inner->boundingRect());
                    QVERIFY2(stage.contains(bounds),qPrintable(where+QString(": %1 %2,%3 %4x%5 leaves the stage %6,%7 %8x%9").arg(inner->objectName()).arg(bounds.x()).arg(bounds.y()).arg(bounds.width()).arg(bounds.height()).arg(stage.x()).arg(stage.y()).arg(stage.width()).arg(stage.height())));
                }
                QVERIFY2(text->height()>=text->implicitHeight()-1,qPrintable(where));
                const auto picture=window->grabWindow();
                const QColor red("#f2361f");
                const qreal ratio=picture.devicePixelRatio();
                for(const auto &corner:{stage.bottomLeft()+QPointF(1,-2),stage.bottomRight()+QPointF(-2,-2)}) {
                    const QColor pixel=picture.pixelColor((corner*ratio).toPoint());
                    QVERIFY2(qAbs(pixel.red()-red.red())<4 && qAbs(pixel.green()-red.green())<4,qPrintable(where+": corner shows "+pixel.name()));
                }
                capture(QString("launch/%1x%2-%3-%4").arg(size.width()).arg(size.height()).arg(language).arg(panel?"panel":"nopanel"));
            }
        }
        backend->setLanguage("en");view->setProperty("activityForced",false);view->setProperty("showActivity",activity);window->resize(1380,970);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    // Every main screen, dialog and device state at each size, in each theme and language:
    // nothing that can be clicked leaves the window. With TOMACCO_UI_CAPTURES set this walks the
    // whole matrix and saves it; otherwise the two corners that break first.
    void every_screen_fits_every_size_theme_and_language() {
        const bool capturing=!qEnvironmentVariable("TOMACCO_UI_CAPTURES").isEmpty();
        struct Variant {QSize size;const char *theme;const char *language;};
        QList<Variant> variants;
        // Narrow, tall windows are normal in a tiling window manager. German: the longest text.
        for(const int width:{900,800,700,600,560})for(const int height:{1040,800,600})variants.append({QSize(width,height),"dark","de"});
        variants.append({QSize(560,480),"light","en"});variants.append({QSize(1280,800),"light","en"});
        // The other languages at the sizes that break first.
        for(const auto *language:{"fr","es","it"})for(const auto &size:{QSize(560,1040),QSize(700,600),QSize(900,800)})variants.append({size,"dark",language});
        if(capturing) {
            variants.clear();
            for(const auto &size:{QSize(1920,1080),QSize(1280,800),QSize(900,700),QSize(600,1040)})for(const auto *theme:{"dark","light"})for(const auto *language:{"en","de"})variants.append({size,theme,language});
        }
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *display=qobject_cast<NativeView *>(object("nativeDisplay"));QVERIFY(display);
        view->setProperty("autoAttach",false);
        QLocalServer video,input;
        QVERIFY(video.listen(directory.filePath("matrix-video")));QVERIFY(input.listen(directory.filePath("matrix-input")));
        // A plain app screen for the device to show.
        QImage picture(QSize(1080,2424),QImage::Format_RGB32);picture.fill(QColor("#f3f2ee"));
        {QPainter painter(&picture);painter.fillRect(96,260,520,72,QColor("#141412"));for(int i=0;i<4;++i)painter.fillRect(96,420+i*150,888,96,QColor("#d6d4cb"));painter.fillRect(96,1960,888,150,QColor("#f2361f"));}
        QByteArray bytes;{QBuffer output(&bytes);output.open(QIODevice::WriteOnly);picture.save(&output,"JPEG");}
        QByteArray packet(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;
        connect(&video,&QLocalServer::newConnection,this,[&]{auto *socket=video.nextPendingConnection();socket->write(packet);connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        connect(&input,&QLocalServer::newConnection,this,[&]{auto *socket=input.nextPendingConnection();connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        const auto fits=[&](const QString &where){
            std::function<void(QQuickItem *)> walk=[&](QQuickItem *item){
                if(!item->isVisible())return;
                // A row that cannot fit shows as children past its end, or on top of each other.
                if(item->inherits("QQuickRowLayout") && item->width()>0) {
                    qreal end=-1;
                    for(auto *child:item->childItems()) {
                        if(!child->isVisible() || child->width()<=0)continue;
                        QVERIFY2(child->x()+.5>=end,qPrintable(QString("%1: %2 overlaps in a row, x %3 after %4").arg(where,child->objectName()+" "+child->property("text").toString()).arg(child->x()).arg(end)));
                        end=child->x()+child->width();
                    }
                    QVERIFY2(end<=item->width()+1,qPrintable(QString("%1: row ends at %2 of %3, last \"%4\"").arg(where).arg(end).arg(item->width()).arg(item->childItems().isEmpty()?QString():item->childItems().constLast()->property("text").toString())));
                }
                if(QString::fromLatin1(item->metaObject()->className()).startsWith("StudioButton_QMLTYPE") && item->width()>0) {
                    const auto bounds=item->mapRectToScene(item->boundingRect());
                    QVERIFY2(bounds.left()>=-.5 && bounds.right()<=window->width()+.5,
                        qPrintable(QString("%1: \"%2\" spans %3..%4 in a %5-wide window").arg(where,item->property("text").toString()).arg(bounds.left()).arg(bounds.right()).arg(window->width())));
                }
                for(auto *child:item->childItems())walk(child);
            };
            walk(window->contentItem());
        };
        const auto shot=[&](const QString &variant,const QString &name){QTest::qWait(capturing?120:30);fits(variant+"/"+name);if(QTest::currentTestFailed())return;capture(variant+"/"+name);};
        const auto dialog=[&](const QString &variant,const char *name,const QString &shotName){
            auto *popup=object(name);QVERIFY(popup);
            QVERIFY(QMetaObject::invokeMethod(popup,"open"));QTRY_VERIFY(popup->property("visible").toBool());
            QVERIFY2(popup->property("height").toReal()<=window->height() && popup->property("y").toReal()>=-1 && popup->property("width").toReal()<=window->width(),qPrintable(variant+"/"+shotName+" does not fit the window"));
            shot(variant,shotName);
            QVERIFY(QMetaObject::invokeMethod(popup,"close"));QTRY_VERIFY(!popup->property("visible").toBool());
        };
        for(const auto &[size,theme,language]:variants) {
            const auto variant=QString("%1-%2-%3").arg(size.width()).arg(theme).arg(language);
            backend->setAppearance(theme);backend->setLanguage(language);
            window->resize(size);QTest::qWait(60);
            // Nothing attached: the first thing a new user sees.
            view->setProperty("live",QVariantMap{});display->setProperty("session",QVariantMap{});view->setProperty("notice","");
            view->setProperty("targets",QVariantList{});workspace->setProperty("jobs",QVariantList{});
            view->setProperty("activityForced",false);view->setProperty("showActivity",true);
            open("runtime");shot(variant,"device-empty");
            for(const auto *tab:{"project","devices","jobs","tools","debug","testing","profiling","files","release"}){open(QString::fromLatin1(tab));shot(variant,QString::fromLatin1(tab));}
            QVERIFY(QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("ios"))));open("devices");shot(variant,"devices-apple");
            QVERIFY(QMetaObject::invokeMethod(workspace,"chooseDevicePlatform",Q_ARG(QVariant,QString("android"))));
            // A device starting up, with an agent's job running.
            open("runtime");
            const double now=QDateTime::currentSecsSinceEpoch();
            const QVariantMap build{{"id","matrix-build"},{"label","Build Android · debug"},{"state","running"},{"phase","building"},{"created",now-75},{"updated",now},
                {"plan",QVariantMap{{"steps",QVariantList{QVariantMap{{"argv",QVariantList{"/usr/bin/flutter","build","apk","--debug"}}}}}}}};
            const QVariantMap done{{"id","matrix-done"},{"label","Run tests on Pixel 11"},{"state","succeeded"},{"created",now-400},{"updated",now-300}};
            const QVariantMap failed{{"id","matrix-failed"},{"label","Lint"},{"state","failed"},{"created",now-900},{"updated",now-880}};
            workspace->setProperty("jobs",QVariantList{build,done,failed});
            auto *panel=object("activityPanel");QVERIFY(panel);
            panel->setProperty("logTail","Running Gradle task 'assembleDebug'...\n✓ Built build/app/outputs/flutter-apk/app-debug.apk");
            QVERIFY(QMetaObject::invokeMethod(panel,"noteCall",Q_ARG(QVariant,QString("configure"))));
            view->setProperty("pendingAvd","Tomacco-pixel-11");
            shot(variant,"device-booting");
            view->setProperty("pendingAvd","");
            // A live device.
            const QVariantMap target{{"id","matrix-device"},{"name","Pixel 11 emulator"},{"platform","android"},{"kind","emulator"},{"profile_id","pixel-11"},{"profile_source","assigned"}};
            view->setProperty("targets",QVariantList{target});
            const QVariantMap session{{"session",QString("matrix-")+variant},{"platform","android"},{"status","connected"},{"device","matrix-device"},{"codec","mjpeg"},{"video_socket",video.fullServerName()},{"control_socket",input.fullServerName()},
                {"audio",QVariantMap{{"enabled",true},{"configured",true},{"status","running"}}}};
            QVERIFY(QMetaObject::invokeMethod(view,"attach",Q_ARG(QVariant,session)));
            QTRY_COMPARE_WITH_TIMEOUT(display->state(),QString("live"),patient(3000));
            workspace->setProperty("jobs",QVariantList{build,done,failed});
            shot(variant,"device-live");
            view->setProperty("notice","Building the app and launching it on the device…");shot(variant,"device-notice");view->setProperty("notice","");
            open("jobs");QTest::qWait(capturing?400:0);workspace->setProperty("jobs",QVariantList{build,done,failed});workspace->setProperty("selectedJob","matrix-build");workspace->setProperty("activeJob",build);workspace->setProperty("logText","Running Gradle task 'assembleDebug'...\n✓ Built build/app/outputs/flutter-apk/app-debug.apk\n");shot(variant,"jobs-running");
            workspace->setProperty("selectedJob","");workspace->setProperty("activeJob",QVariantMap{});workspace->setProperty("logText","");
            view->setProperty("live",QVariantMap{});display->setProperty("session",QVariantMap{});workspace->setProperty("jobs",QVariantList{});panel->setProperty("calls",QVariantList{});panel->setProperty("logTail","");
            QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
            for(const auto &[name,shotName]:{std::pair{"commandPalette","dialog-palette"},{"settingsDialog","dialog-settings"},{"shortcutsDialog","dialog-shortcuts"},{"aboutDialog","dialog-about"},{"iphoneSetup","dialog-iphone"}})dialog(variant,name,QString::fromLatin1(shotName));
            open("devices");
            for(const auto &[name,shotName]:{std::pair{"createAndroidDialog","dialog-android-add"},{"androidPerformanceDialog","dialog-android-performance"}})dialog(variant,name,QString::fromLatin1(shotName));
            // The separate web preview.
            window->setProperty("nativeMode",false);shot(variant,"web-preview");window->setProperty("nativeMode",true);
            QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
            if(QTest::currentTestFailed())return;
        }
        backend->setAppearance("system");backend->setLanguage("en");window->resize(1380,970);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void a_missing_or_broken_control_program_is_explained() {
        // Missing: known before any request, and every request answers with a sentence instead of a raw error.
        Control missing("ui-missing-cli",nullptr,directory.filePath("no-such/tomaccoctl"));
        QVERIFY(missing.cliError().contains("tomaccoctl"));
        QSignalSpy replies(&missing,&Control::apiFinished);
        missing.request("doctor");
        QTRY_VERIFY_WITH_TIMEOUT(replies.size()>=2,patient(5000));
        for(const auto &reply:replies) {
            const auto error=reply.at(2).toMap().value("error").toMap();
            QVERIFY(!reply.at(2).toMap().value("ok").toBool());
            QVERIFY2(error.value("message").toString().contains("Reinstall Tomacco"),qPrintable(error.value("message").toString()));
        }
        QVERIFY(missing.operations().isEmpty());QVERIFY(missing.cliVersion().isEmpty());
        // Broken: a program that only prints a crash to stderr.
        const auto broken=directory.filePath("broken-tomaccoctl");
        {QFile script(broken);QVERIFY(script.open(QIODevice::WriteOnly));script.write("#!/bin/sh\necho 'thread main panicked' >&2\nexit 101\n");}
        QFile::setPermissions(broken,QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner);
        Control failing("ui-broken-cli",nullptr,broken);
        QVERIFY(failing.cliError().isEmpty());
        QTRY_VERIFY_WITH_TIMEOUT(!failing.cliError().isEmpty(),patient(5000));
        QVERIFY2(failing.cliError().contains("not working") && failing.cliError().contains("panicked"),qPrintable(failing.cliError()));
        // The desktop says so where the user looks first, instead of an empty, healthy-looking workspace.
        QQmlApplicationEngine second;
        auto *context=second.rootContext();
        context->setContextProperty("Studio",backend.get());context->setContextProperty("Agent",&missing);
        context->setContextProperty("initialProject",QString());context->setContextProperty("initialWorkspace","runtime");
        context->setContextProperty("initialDevice","");
        for(const auto &key:{"smokeTest","captureMode","initialLandscape","initialUnfolded","initialDeviceOnly"})context->setContextProperty(key,false);
        QStringList errors;
        connect(&second,&QQmlEngine::warnings,this,[&errors](const QList<QQmlError> &list){for(const auto &e:list)if(e.url().toString().startsWith("qrc:/qml/"))errors.append(e.toString());});
        second.load(QUrl("qrc:/qml/Main.qml"));
        QVERIFY(!second.rootObjects().isEmpty());
        auto *other=qobject_cast<QQuickWindow *>(second.rootObjects().first());QVERIFY(other);
        QVERIFY(QTest::qWaitForWindowExposed(other));
        auto *view=other->findChild<QObject *>("nativeWorkspace");QVERIFY(view);
        QTRY_COMPARE(view->property("notice").toString(),missing.cliError());QVERIFY(view->property("failed").toBool());
        auto *status=other->findChild<QObject *>("agentStatus");QVERIFY(status);QVERIFY(!status->property("ready").toBool());
        if(!qEnvironmentVariable("TOMACCO_UI_CAPTURES").isEmpty())QVERIFY(other->grabWindow().save(qEnvironmentVariable("TOMACCO_UI_CAPTURES")+"/missing-control-program.png"));
        other->close();
        QVERIFY2(errors.isEmpty(),qPrintable(errors.join('\n')));
    }
    void job_log_filters_by_text_and_level_and_copies_the_lines() {
        // The reader narrows a job's log to a text and a level, and copies just those lines.
        open("jobs");window->resize(1380,970);QTest::qWait(50);
        const double now=QDateTime::currentSecsSinceEpoch();
        workspace->setProperty("jobs",QVariantList{QVariantMap{{"id","log-filter-job"},{"label","Build Android · debug"},{"state","failed"},{"created",now-40},{"updated",now}}});
        workspace->setProperty("selectedJob",QString("log-filter-job"));
        workspace->setProperty("logText",QString("Resolving dependencies\nwarning: 'foo' is deprecated\nerror: build failed in app.dart\nE/AndroidRuntime: FATAL EXCEPTION: main\n10-31 12:00:00.000  1234  1234 W Choreographer: Skipped 30 frames\nBUILD done\n"));
        auto *search=item("logSearch");QVERIFY(search);QTRY_VERIFY(search->isVisible());
        // The bar takes its place above the log once the layout has run.
        QTRY_VERIFY(qobject_cast<QQuickItem *>(object("jobLog"))->y()>=item("logFilterBar")->y()+item("logFilterBar")->height());
        const auto shown=[&]{return workspace->property("shownLog").toString();};
        QCOMPARE(shown().count('\n'),6);
        search->forceActiveFocus();type("BUILD");
        QTRY_COMPARE(shown(),QString("error: build failed in app.dart\nBUILD done"));
        QCOMPARE(item("logMatches")->property("text").toString(),QString("2 lines"));
        // Errors only, then warnings and errors, within the text.
        workspace->setProperty("logQuery",QString());
        auto *errors=item("logLevel-error");QVERIFY(errors);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,errors->mapToScene(QPointF(errors->width()/2,errors->height()/2)).toPoint());
        QTRY_COMPARE(shown(),QString("error: build failed in app.dart\nE/AndroidRuntime: FATAL EXCEPTION: main"));
        workspace->setProperty("logLevel",QString("warning"));
        QCOMPARE(shown(),QString("warning: 'foo' is deprecated\nerror: build failed in app.dart\nE/AndroidRuntime: FATAL EXCEPTION: main\n10-31 12:00:00.000  1234  1234 W Choreographer: Skipped 30 frames"));
        // Copy takes exactly the lines shown.
        QGuiApplication::clipboard()->clear();
        auto *copy=item("logCopy");QVERIFY(copy);QCOMPARE(copy->property("text").toString(),QString("Copy these lines"));
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,copy->mapToScene(QPointF(copy->width()/2,copy->height()/2)).toPoint());
        QTRY_COMPARE(QGuiApplication::clipboard()->text(),shown());
        // Nothing matching says so instead of looking empty.
        workspace->setProperty("logQuery",QString("no such text"));
        QCOMPARE(shown(),QString());
        QCOMPARE(object("jobLog")->property("placeholderText").toString(),QString("No line of this log matches."));
        workspace->setProperty("logQuery",QString());workspace->setProperty("logLevel",QString("all"));
        QCOMPARE(copy->property("text").toString(),QString("Copy log"));
        workspace->setProperty("selectedJob",QString());workspace->setProperty("logText",QString());workspace->setProperty("jobs",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void jobs_history_shows_duration_and_project_filters_and_runs_again() {
        // Each job shows its state, how long it ran and its project; the list narrows to running or
        // failed jobs; a job this window started can be started again, one from elsewhere cannot.
        open("jobs");window->resize(1380,970);QTest::qWait(50);
        const double now=QDateTime::currentSecsSinceEpoch();
        const auto project=directory.filePath("project");
        const QVariantMap running{{"id","history-running"},{"label","Build Android · debug"},{"state","running"},{"created",now-75},{"updated",now},{"plan",QVariantMap{{"exclusive",project}}}};
        const QVariantMap failed{{"id","history-failed"},{"label","Lint"},{"state","failed"},{"created",now-900},{"updated",now-780},{"plan",QVariantMap{{"exclusive",project}}},{"log_path","/nonexistent/history-failed.log"}};
        const QVariantMap done{{"id","history-done"},{"label","Install app"},{"state","succeeded"},{"created",now-4000},{"updated",now-3990},{"plan",QVariantMap{{"exclusive",""}}}};
        workspace->setProperty("jobs",QVariantList{running,failed,done});
        QTRY_VERIFY(item("jobRow-history-failed"));
        const auto duration=[&](const QVariantMap &job){QVariant out;QMetaObject::invokeMethod(workspace,"jobDuration",Q_RETURN_ARG(QVariant,out),Q_ARG(QVariant,job));return out.toString();};
        QCOMPARE(duration(failed),QString("2 min 0 s"));QCOMPARE(duration(done),QString("10 s"));
        QTRY_VERIFY(duration(running).startsWith("1 min"));
        auto *row=item("jobRow-history-failed");
        QCOMPARE(row->property("project").toString(),QString("project"));
        QCOMPARE(item("jobRow-history-done")->property("project").toString(),QString());
        // Filters.
        const auto shown=[&]{return workspace->property("shownJobs").value<QJSValue>().property("length").toInt();};
        QCOMPARE(shown(),3);
        auto *failedFilter=item("jobFilter-failed");QVERIFY(failedFilter);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,failedFilter->mapToScene(QPointF(failedFilter->width()/2,failedFilter->height()/2)).toPoint());
        QTRY_COMPARE(shown(),1);QTRY_VERIFY(!item("jobRow-history-done") || !item("jobRow-history-done")->isVisible());
        workspace->setProperty("jobFilter",QString("running"));QCOMPARE(shown(),1);
        QCOMPARE(workspace->property("shownJobs").value<QJSValue>().property(0).property("id").toString(),QString("history-running"));
        workspace->setProperty("jobFilter",QString("all"));QCOMPARE(shown(),3);
        // A job from another client cannot be run again; a running one neither.
        QVERIFY(QMetaObject::invokeMethod(workspace,"selectJob",Q_ARG(QVariant,failed)));
        auto *rerun=item("jobRerun");QVERIFY(rerun);QTRY_VERIFY(rerun->isVisible());
        QVERIFY(!rerun->isEnabled());QVERIFY(item("jobLogFile")->isEnabled());
        // A job this window started is remembered with its request, also across a restart.
        workspace->setProperty("pending",QVariantMap{{"-71",QVariantMap{{"operation","job.get"},{"quiet",true},{"signature",QString(R"({"job_id":"00000000000000000000000000000000"})")}}}});
        QVariantMap started=failed;started["id"]="history-mine";
        control->apiFinished(-71,"job.get",QVariantMap{{"ok",true},{"result",started}});
        QCOMPARE(workspace->property("selectedJob").toString(),QString("history-mine"));
        QVERIFY(QSettings().value("jobs/origins").toString().contains("history-mine"));
        QTRY_VERIFY(rerun->isEnabled());
        workspace->setProperty("pending",QVariantMap{});
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,rerun->mapToScene(QPointF(rerun->width()/2,rerun->height()/2)).toPoint());
        const auto pending=mapOf(workspace->property("pending"));
        QCOMPARE(pending.size(),1);
        QCOMPARE(pending.first().toMap().value("operation").toString(),QString("job.get"));
        QCOMPARE(pending.first().toMap().value("signature").toString(),QString(R"({"job_id":"00000000000000000000000000000000"})"));
        QVariantMap mineRunning=started;mineRunning["state"]="running";
        workspace->setProperty("activeJob",mineRunning);QVERIFY(!rerun->isEnabled());
        QVERIFY(item("jobCancel")->isEnabled());
        // The (failing) request ends; nothing is left behind for the other scenarios.
        QTRY_VERIFY_WITH_TIMEOUT(mapOf(workspace->property("pending")).isEmpty(),patient(10000));
        QSettings().remove("jobs/origins");workspace->setProperty("jobOrigins",QVariantMap{});
        workspace->setProperty("selectedJob",QString());workspace->setProperty("activeJob",QVariantMap{});workspace->setProperty("jobs",QVariantList{});
        workspace->setProperty("status",QString());workspace->setProperty("failed",false);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void a_job_that_ends_in_the_background_is_notified() {
        // A job that ran a while and ends while no window of the app is active becomes a desktop
        // notification (here only the signal: the offscreen test platform sends none); clicking it
        // opens the job. Short, cancelled and foreground jobs stay quiet, and the setting turns it off.
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        QSignalSpy sent(backend.get(),&Backend::notificationSent);
        view->setProperty("appActive",false);
        const double now=QDateTime::currentSecsSinceEpoch();
        const auto job=[&](const char *id,const char *state,double ran){return QVariantMap{{"id",id},{"label",QString("Job ")+id},{"state",state},{"created",now-ran},{"updated",now}};};
        workspace->setProperty("jobs",QVariantList{job("notify-long","running",60),job("notify-short","running",3),job("notify-cancel","running",60),job("notify-timeout","queued",120)});
        QCOMPARE(sent.size(),0);
        workspace->setProperty("jobs",QVariantList{job("notify-long","succeeded",60),job("notify-short","failed",3),job("notify-cancel","cancelled",60),job("notify-timeout","timed_out",120)});
        QCOMPARE(sent.size(),2);
        QCOMPARE(sent.at(0).at(0).toString(),QString("Job notify-long"));
        QCOMPARE(sent.at(0).at(1).toString(),QString("Completed after 1 min 0 s"));
        QCOMPARE(sent.at(0).at(2).toString(),QString("notify-long"));
        QCOMPARE(sent.at(1).at(1).toString(),QString("Timed out after 2 min 0 s"));
        // A job seen only once it has ended was not watched running: no notification.
        workspace->setProperty("jobs",QVariantList{job("notify-long","succeeded",60),job("notify-new","failed",60)});
        QCOMPARE(sent.size(),2);
        // In the foreground nothing is sent.
        view->setProperty("appActive",true);
        workspace->setProperty("jobs",QVariantList{job("notify-fg","running",60)});workspace->setProperty("jobs",QVariantList{job("notify-fg","failed",60)});
        QCOMPARE(sent.size(),2);
        // The setting turns it off, and is stored.
        view->setProperty("appActive",false);
        auto *dialog=object("settingsDialog");QVERIFY(dialog);
        QVERIFY(QMetaObject::invokeMethod(dialog,"open"));QTRY_VERIFY(dialog->property("visible").toBool());
        auto *toggle=item("jobNotificationsToggle");QVERIFY(toggle);QVERIFY(toggle->property("checked").toBool());
        QTRY_VERIFY(toggle->height()>0);
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,toggle->mapToScene(QPointF(Theme_iconHalf,toggle->height()/2)).toPoint());
        QTRY_VERIFY(!view->property("notifyJobs").toBool());
        QTRY_COMPARE(QSettings().value("Notifications/jobs").toString(),QString("false"));
        QVERIFY(QMetaObject::invokeMethod(dialog,"close"));QTRY_VERIFY(!dialog->property("visible").toBool());
        workspace->setProperty("jobs",QVariantList{job("notify-off","running",60)});workspace->setProperty("jobs",QVariantList{job("notify-off","failed",60)});
        QCOMPARE(sent.size(),2);
        view->setProperty("notifyJobs",true);
        workspace->setProperty("jobs",QVariantList{job("notify-on","running",60)});workspace->setProperty("jobs",QVariantList{job("notify-on","failed",60)});
        QCOMPARE(sent.size(),3);QCOMPARE(sent.at(2).at(1).toString(),QString("Failed after 1 min 0 s"));
        // Clicking the notification opens the job.
        emit backend->notificationActivated("notify-on");
        QTRY_COMPARE(workspace->property("section").toString(),QString("jobs"));
        QVERIFY(workspace->property("opened").toBool());
        QCOMPARE(workspace->property("selectedJob").toString(),QString("notify-on"));
        view->setProperty("appActive",true);
        workspace->setProperty("selectedJob",QString());workspace->setProperty("activeJob",QVariantMap{});workspace->setProperty("jobs",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    // What update.status says when a newer release can be installed.
    static QVariantMap updateStatus(const QString &latest) {
        return {{"mode","notify"},{"state","update_available"},{"update_available",true},{"latest",latest},
            {"current",QCoreApplication::applicationVersion()},{"can_apply",true},{"notes_url","https://github.com/marselnenaj/tomacco/releases/tag/v"+latest},
            {"install",QVariantMap{{"kind","user_install"}}}};
    }
    void clickItem(const char *name) {
        auto *target=item(name);QVERIFY2(target,name);QTRY_VERIFY2(target->isVisible() && target->width()>0,name);
        // A row that just changed lays itself out again before the next frame.
        QTest::qWait(patient(100));
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,target->mapToScene(QPointF(target->width()/2,target->height()/2)).toPoint());
    }
    void update_banner_offers_a_new_version_once_and_later_puts_it_away() {
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *updates=object("updateModel");QVERIFY(updates);
        auto *banner=item("updateBanner");QVERIFY(banner);
        // The model's own checks and their answers would replace the status set here.
        updates->setProperty("background",false);
        QTRY_VERIFY_WITH_TIMEOUT(mapOf(updates->property("requests")).isEmpty(),patient(15000));
        QSettings().remove("updates");updates->setProperty("dismissed",QString());
        QSignalSpy sent(backend.get(),&Backend::notificationSent);
        QSignalSpy asked(updates,SIGNAL(asked(QString,QVariant)));
        // Found while the window is in the background: the banner and one desktop notification.
        view->setProperty("appActive",false);
        updates->setProperty("status",updateStatus("9.0.0"));
        QTRY_VERIFY(banner->isVisible());
        // Laid out below the title bar, across the window.
        QTRY_VERIFY(banner->width()==view->property("width").toDouble() && banner->height()>0 && banner->mapToScene(QPointF(0,0)).y()>0);
        QVERIFY(object("updateBannerText")->property("text").toString().contains("9.0.0"));
        QVERIFY(item("updateBannerNotes")->isVisible());
        QVERIFY(object("statusUpdate")->property("visible").toBool());
        QCOMPARE(sent.size(),1);QCOMPARE(sent.at(0).at(2).toString(),QString("update:9.0.0"));
        // The same version again sends nothing more.
        updates->setProperty("status",QVariantMap{});QTRY_VERIFY(!banner->isVisible());
        updates->setProperty("status",updateStatus("9.0.0"));QTRY_VERIFY(banner->isVisible() && banner->width()>0);
        QCOMPARE(sent.size(),1);
        // Install asks tomaccoctl for update.apply, as the user's request.
        view->setProperty("appActive",true);
        const auto applied=[&]{for(const auto &call:asked)if(call.at(0).toString()=="update.apply")return mapOf(call.at(1));return QVariantMap{};};
        asked.clear();
        clickItem("updateBannerInstall");
        QTRY_VERIFY(!applied().isEmpty());
        QVERIFY(applied().value("manual").toBool());
        // The forced-off control program refuses; its answer refreshes the status.
        QTRY_VERIFY_WITH_TIMEOUT(!updates->property("working").toBool(),patient(15000));
        QTRY_VERIFY_WITH_TIMEOUT(mapOf(updates->property("requests")).isEmpty(),patient(15000));
        // Later puts the banner away for this version only; the status line keeps its word.
        updates->setProperty("status",updateStatus("9.0.0"));QTRY_VERIFY(banner->isVisible() && banner->width()>0);
        clickItem("updateBannerLater");
        QTRY_VERIFY(!banner->isVisible());
        QCOMPARE(QSettings().value("updates/dismissedVersion").toString(),QString("9.0.0"));
        QVERIFY(object("statusUpdate")->property("visible").toBool());
        updates->setProperty("status",updateStatus("9.1.0"));
        QTRY_VERIFY(banner->isVisible());
        // Off: no banner, whatever a manual check found.
        auto off=updateStatus("9.2.0");off["mode"]="off";updates->setProperty("status",off);
        QTRY_VERIFY(!banner->isVisible());
        updates->setProperty("status",QVariantMap{});updates->setProperty("background",true);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    // What update.status says once a newer version is installed and this desktop is still the old one.
    static QVariantMap installedStatus(const QString &version) {
        return {{"mode","auto"},{"state","up_to_date"},{"update_available",false},{"latest",version},{"current",version},
            {"can_apply",true},{"install",QVariantMap{{"kind","user_install"}}},{"last_install",QVariantMap{{"to",version}}}};
    }
    void a_background_install_asks_for_a_restart_and_settings_explain_each_mode() {
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *updates=object("updateModel");QVERIFY(updates);
        auto *banner=item("updateBanner");QVERIFY(banner);
        updates->setProperty("background",false);
        QTRY_VERIFY_WITH_TIMEOUT(mapOf(updates->property("requests")).isEmpty(),patient(15000));
        QSettings().remove("updates");updates->setProperty("dismissed",QString());
        QVERIFY(QCoreApplication::applicationVersion()!="9.9.0");
        updates->setProperty("status",installedStatus("9.9.0"));
        QTRY_VERIFY(banner->isVisible() && banner->width()>0);
        QCOMPARE(updates->property("bannerKind").toString(),QString("restart"));
        QVERIFY(object("updateBannerText")->property("text").toString().startsWith("Updated to 9.9.0"));
        QVERIFY(item("updateBannerRestart")->isVisible());QVERIFY(!item("updateBannerInstall")->isVisible());
        // Later puts the restart band away for this version.
        clickItem("updateBannerLater");QTRY_VERIFY(!banner->isVisible());
        QCOMPARE(QSettings().value("updates/dismissedVersion").toString(),QString("9.9.0"));
        // The Settings explain every mode in a line of its own, the chosen one stronger.
        auto *dialog=object("settingsDialog");QVERIFY(dialog);
        QVERIFY(QMetaObject::invokeMethod(dialog,"open"));QTRY_VERIFY(dialog->property("visible").toBool());
        for(const auto *mode:{"off","notify","auto"}) {
            auto *line=item(qPrintable(QString("updateModeHelp-")+mode));QVERIFY2(line,mode);
            QVERIFY(!line->property("text").toString().isEmpty());
            QCOMPARE(line->property("chosen").toBool(),QString(mode)=="auto");
        }
        QVERIFY(QMetaObject::invokeMethod(dialog,"close"));QTRY_VERIFY(!dialog->property("visible").toBool());
        updates->setProperty("status",QVariantMap{});updates->setProperty("background",true);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void restart_now_starts_the_installed_desktop_with_this_project_and_view() {
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *updates=object("updateModel");QVERIFY(updates);
        auto *banner=item("updateBanner");QVERIFY(banner);
        updates->setProperty("background",false);
        QTRY_VERIFY_WITH_TIMEOUT(mapOf(updates->property("requests")).isEmpty(),patient(15000));
        QSettings().remove("updates");updates->setProperty("dismissed",QString());
        // The re-exec is recorded, not run.
        QString program;QStringList arguments;QProcessEnvironment environment;int starts=0;
        backend->setStarter([&](const QString &p,const QStringList &a,const QProcessEnvironment &e){program=p;arguments=a;environment=e;++starts;return true;});
        const auto cleanup=qScopeGuard([&]{backend->setStarter({});view->setProperty("quitting",false);updates->setProperty("status",QVariantMap{});updates->setProperty("background",true);});
        QVERIFY(QMetaObject::invokeMethod(workspace,"showTab",Q_ARG(QVariant,"jobs")));
        QTRY_COMPARE(workspace->property("section").toString(),QString("jobs"));
        updates->setProperty("status",installedStatus("9.9.0"));
        QTRY_VERIFY(banner->isVisible() && banner->width()>0);
        const int before=quits;
        clickItem("updateBannerRestart");
        QTRY_COMPARE(starts,1);
        QCOMPARE(program,Backend::relaunchProgram(QCoreApplication::applicationFilePath(),QDir::homePath()));
        QCOMPARE(arguments.mid(arguments.indexOf("--project"),2),(QStringList{"--project",directory.filePath("project")}));
        QCOMPARE(arguments.mid(arguments.indexOf("--workspace"),2),(QStringList{"--workspace","jobs"}));
        QCOMPARE(environment.value("TOMACCO_RESTART_AFTER"),QString::number(QCoreApplication::applicationPid()));
        // This desktop then ends, with its window's size kept for the new one.
        QTRY_COMPARE(quits,before+1);
        QVERIFY(QSettings().contains("window/width"));
        // A start that fails keeps this desktop and says so.
        view->setProperty("quitting",false);view->setProperty("notice",QString());
        backend->setStarter([](const QString &,const QStringList &,const QProcessEnvironment &){return false;});
        QVERIFY(QMetaObject::invokeMethod(updates,"restartRequested"));
        QTRY_VERIFY(view->property("failed").toBool());
        QCOMPARE(quits,before+1);
        view->setProperty("notice",QString());view->setProperty("failed",false);
        QVERIFY(QMetaObject::invokeMethod(workspace,"close"));
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void whats_new_shows_once_on_the_first_start_of_a_new_version() {
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *sheet=object("whatsNewSheet");QVERIFY(sheet);
        QVERIFY(!sheet->property("visible").toBool());
        // The same version again: nothing.
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,QVariant())));QTest::qWait(50);
        QVERIFY(!sheet->property("visible").toBool());
        // An older version started here before: the sheet, once.
        QSettings().setValue("app/lastVersion","1.1.0");
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,QVariant())));
        QTRY_VERIFY(sheet->property("visible").toBool());
        QVERIFY(sheet->property("title").toString().contains(QCoreApplication::applicationVersion()));
        QCOMPARE(QSettings().value("app/lastVersion").toString(),QCoreApplication::applicationVersion());
        QVERIFY(QMetaObject::invokeMethod(sheet,"close"));QTRY_VERIFY(!sheet->property("visible").toBool());
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,QVariant())));QTest::qWait(50);
        QVERIFY(!sheet->property("visible").toBool());
        // Going back to an older version (a rollback) shows nothing.
        QSettings().setValue("app/lastVersion","99.0.0");
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,QVariant())));QTest::qWait(50);
        QVERIFY(!sheet->property("visible").toBool());
        QVariant newer;
        QVERIFY(QMetaObject::invokeMethod(view,"newerVersion",Q_RETURN_ARG(QVariant,newer),Q_ARG(QVariant,"1.2.0"),Q_ARG(QVariant,"1.2.0-rc.1")));QVERIFY(newer.toBool());
        QVERIFY(QMetaObject::invokeMethod(view,"newerVersion",Q_RETURN_ARG(QVariant,newer),Q_ARG(QVariant,"1.10.0"),Q_ARG(QVariant,"1.9.3")));QVERIFY(newer.toBool());
        QVERIFY(QMetaObject::invokeMethod(view,"newerVersion",Q_RETURN_ARG(QVariant,newer),Q_ARG(QVariant,"1.1.0"),Q_ARG(QVariant,"1.1.0")));QVERIFY(!newer.toBool());
        QSettings().setValue("app/lastVersion",QCoreApplication::applicationVersion());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void onboarding_shows_once_on_the_first_start_and_from_the_palette() {
        open("runtime");
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *sheet=object("onboardingSheet");QVERIFY(sheet);
        auto *whatsNew=object("whatsNewSheet");QVERIFY(whatsNew);
        QVERIFY(!sheet->property("visible").toBool());
        // The very first start: no version recorded and no settings before.
        QSettings().remove("app/lastVersion");QSettings().remove("onboarding/shown");
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,true)));
        QTRY_VERIFY(sheet->property("visible").toBool());
        QVERIFY(!whatsNew->property("visible").toBool());
        QCOMPARE(QSettings().value("onboarding/shown").toString(),QString("true"));
        // doctor's next steps, in plain words, or that nothing is left.
        QTRY_VERIFY_WITH_TIMEOUT(!sheet->property("checking").toBool(),patient(60000));
        const auto steps=sheet->property("steps").toStringList();
        QCOMPARE(item("onboardingReady")->isVisible(),steps.isEmpty());
        for(const auto &step:steps)QVERIFY2(!step.contains("tomaccoctl setup --yes") && !step.contains("pacman"),qPrintable(step));
        // Connect copies the MCP configuration.
        QGuiApplication::clipboard()->clear();
        clickItem("onboardingConnect");
        QTRY_VERIFY(QGuiApplication::clipboard()->text().contains("\"mcpServers\""));
        QVERIFY(QGuiApplication::clipboard()->text().contains(control->cliPath()));
        QVERIFY(item("onboardingCopied")->isVisible());
        clickItem("onboardingSkip");QTRY_VERIFY(!sheet->property("visible").toBool());
        // Not again by itself, nor as a new version's sheet.
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,true)));QTest::qWait(50);
        QVERIFY(!sheet->property("visible").toBool());QVERIFY(!whatsNew->property("visible").toBool());
        QSettings().remove("app/lastVersion");
        QVERIFY(QMetaObject::invokeMethod(view,"startupSheets",Q_ARG(QVariant,true)));QTest::qWait(50);
        QVERIFY(!sheet->property("visible").toBool());
        // The palette opens it again.
        QTest::keyClick(window,Qt::Key_K,Qt::ControlModifier);QTRY_VERIFY(item("commandSearch")->hasActiveFocus());
        type("Get started");QTest::keyClick(window,Qt::Key_Return);
        QTRY_VERIFY(sheet->property("visible").toBool());
        QTRY_VERIFY_WITH_TIMEOUT(!sheet->property("checking").toBool(),patient(60000));
        QVERIFY(QMetaObject::invokeMethod(sheet,"close"));QTRY_VERIFY(!sheet->property("visible").toBool());
        QSettings().setValue("app/lastVersion",QCoreApplication::applicationVersion());
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void the_session_names_its_version_and_quits_for_another_one() {
        // desktop.open compares this with the installed version.
        const auto state=previewCall("state").value("result").toMap();
        QCOMPARE(state.value("version").toString(),QCoreApplication::applicationVersion());
        auto *view=object("nativeWorkspace");QVERIFY(view);
        const int before=quits;
        const auto reply=previewCall("quit",{{"reason","replaced"}}).value("result").toMap();
        QVERIFY(reply.value("quitting").toBool());QVERIFY(!reply.value("unsaved").toBool());
        QTRY_COMPARE(quits,before+1);
        view->setProperty("quitting",false);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void touches_and_coordinates_show_on_the_live_device() {
        // Switched on, the device view marks a press and names the pointer's device pixel and
        // fraction; the press still reaches the device. Off, nothing is drawn.
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *display=qobject_cast<NativeView *>(object("nativeDisplay"));QVERIFY(display);
        view->setProperty("autoAttach",false);view->setProperty("showDeviceFrame",false);view->setProperty("showTouches",false);
        QLocalServer video,controlSocket;
        QVERIFY(video.listen(directory.filePath("touch-video")));QVERIFY(controlSocket.listen(directory.filePath("touch-input")));
        QImage image(QSize(1000,2000),QImage::Format_RGB32);image.fill(QColor("#295c83"));
        QByteArray bytes;{QBuffer output(&bytes);output.open(QIODevice::WriteOnly);image.save(&output,"JPEG");}
        QByteArray packet(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;
        QVector<QJsonObject> inputs;
        connect(&video,&QLocalServer::newConnection,this,[&]{auto *socket=video.nextPendingConnection();socket->write(packet);connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        connect(&controlSocket,&QLocalServer::newConnection,this,[&]{auto *socket=controlSocket.nextPendingConnection();connect(socket,&QLocalSocket::readyRead,socket,[&,socket]{if(!socket->canReadLine())return;inputs.push_back(QJsonDocument::fromJson(socket->readLine()).object()["arguments"].toObject());socket->write("{\"ok\":true}\n");});connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        view->setProperty("targets",QVariantList{QVariantMap{{"id","emulator-touch"},{"name","Touch test"},{"platform","android"},{"kind","emulator"}}});
        const QVariantMap session{{"session","touch-session"},{"platform","android"},{"device","emulator-touch"},{"codec","mjpeg"},{"video_socket",video.fullServerName()},{"control_socket",controlSocket.fullServerName()}};
        QVERIFY(QMetaObject::invokeMethod(view,"attach",Q_ARG(QVariant,session)));
        QTRY_COMPARE_WITH_TIMEOUT(display->sourceSize(),QSize(1000,2000),patient(3000));
        auto *overlay=item("nativeTouchOverlay");QVERIFY(overlay);QVERIFY(!overlay->isVisible());
        auto *toggle=item("nativeTouchToggle");QVERIFY(toggle);QTRY_VERIFY(toggle->isVisible());
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,toggle->mapToScene(QPointF(toggle->width()/2,toggle->height()/2)).toPoint());
        QTRY_VERIFY(view->property("showTouches").toBool());QTRY_VERIFY(overlay->isVisible());
        QCOMPARE(accessibleName(toggle),QString("Touches and coordinates"));
        // A quarter across and three quarters down the picture.
        const auto rect=display->contentRect();
        const auto at=display->mapToScene(QPointF(rect.x()+rect.width()*.25,rect.y()+rect.height()*.75)).toPoint();
        auto *coordinates=item("nativeTouchCoordinates");QVERIFY(coordinates);
        QTest::mouseMove(window,at);
        QTRY_VERIFY(coordinates->isVisible());
        const auto label=[&]{return item("nativeTouchCoordinatesText")->property("text").toString();};
        const auto numbers=[&]{QStringList parts=label().split(QRegularExpression("[ ,·]+"),Qt::SkipEmptyParts);return parts;};
        QTRY_COMPARE(numbers().size(),4);
        QVERIFY2(qAbs(numbers()[0].toInt()-250)<=2 && qAbs(numbers()[1].toInt()-1500)<=2,qPrintable(label()));
        QVERIFY2(qAbs(numbers()[2].toDouble()-.25)<.003 && qAbs(numbers()[3].toDouble()-.75)<.003,qPrintable(label()));
        // A press shows the mark where the finger is and still goes to the device.
        auto *mark=item("nativeTouchMark");QVERIFY(mark);QVERIFY(!mark->isVisible());
        inputs.clear();
        QTest::mousePress(window,Qt::LeftButton,Qt::NoModifier,at);
        QTRY_VERIFY(mark->isVisible());
        const auto centre=mark->mapToScene(QPointF(mark->width()/2,mark->height()/2));
        QVERIFY(qAbs(centre.x()-at.x())<=1.5 && qAbs(centre.y()-at.y())<=1.5);
        QTest::mouseRelease(window,Qt::LeftButton,Qt::NoModifier,at);
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),2,patient(3000));
        QCOMPARE(inputs[0]["phase"].toString(),QString("down"));
        QVERIFY(qAbs(inputs[0]["x"].toDouble()-.25)<.005);QVERIFY(qAbs(inputs[0]["y"].toDouble()-.75)<.005);
        QTRY_VERIFY(!mark->isVisible());
        // The choice is stored; switched off nothing is drawn and taps still work.
        QTRY_COMPARE(QSettings().value("NativeDisplay/showTouches").toString(),QString("true"));
        view->setProperty("showTouches",false);QVERIFY(!overlay->isVisible());
        inputs.clear();QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,at);
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),2,patient(3000));
        QTest::mouseMove(window,QPoint(window->width()-8,8));
        view->setProperty("live",QVariantMap{});display->setProperty("session",QVariantMap{});view->setProperty("targets",QVariantList{});view->setProperty("showDeviceFrame",true);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void settings_export_and_import_through_a_file() {
        // Settings offers both; an import takes effect at once in the open window.
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        auto *view=object("nativeWorkspace");QVERIFY(view);
        auto *dialog=object("settingsDialog");QVERIFY(dialog);
        QVERIFY(QMetaObject::invokeMethod(dialog,"open"));QTRY_VERIFY(dialog->property("visible").toBool());
        QVERIFY(item("exportSettingsButton"));QVERIFY(item("importSettingsButton"));
        QVERIFY(QMetaObject::invokeMethod(dialog,"close"));QTRY_VERIFY(!dialog->property("visible").toBool());
        QTemporaryDir files;QVERIFY(files.isValid());
        const auto path=files.filePath("tomacco-settings.json");
        view->setProperty("showTouches",true);view->setProperty("notifyJobs",false);view->setProperty("showDeviceFrame",false);
        // Exported at once, before the views have stored the change themselves.
        QVERIFY(QMetaObject::invokeMethod(view,"exportSettingsTo",Q_ARG(QVariant,path)));
        QVERIFY(!view->property("settingsTransferFailed").toBool());
        QCOMPARE(view->property("settingsTransfer").toString(),"Settings saved to "+path);
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));
        const auto values=QJsonDocument::fromJson(file.readAll()).object().value("settings").toObject();file.close();
        QCOMPARE(values.value("NativeDisplay/showTouches"),QJsonValue(true));QCOMPARE(values.value("Notifications/jobs"),QJsonValue(false));
        QVERIFY(!values.contains("window/width"));QVERIFY(!values.contains("recentProjects"));
        // Changed meanwhile; the import brings the exported choices back into the running views.
        view->setProperty("showTouches",false);view->setProperty("notifyJobs",true);view->setProperty("showDeviceFrame",true);
        QVERIFY(QMetaObject::invokeMethod(view,"importSettingsFrom",Q_ARG(QVariant,path)));
        QVERIFY2(!view->property("settingsTransferFailed").toBool(),qPrintable(view->property("settingsTransfer").toString()));
        QVERIFY(view->property("settingsTransfer").toString().endsWith("settings imported"));
        QVERIFY(view->property("showTouches").toBool());QVERIFY(!view->property("notifyJobs").toBool());QVERIFY(!view->property("showDeviceFrame").toBool());
        // A broken file is refused with the reason.
        QVERIFY(file.open(QIODevice::WriteOnly|QIODevice::Truncate));file.write("{}");file.close();
        QVERIFY(QMetaObject::invokeMethod(view,"importSettingsFrom",Q_ARG(QVariant,path)));
        QVERIFY(view->property("settingsTransferFailed").toBool());
        QCOMPARE(view->property("settingsTransfer").toString(),path+" is not a Tomacco settings file.");
        view->setProperty("showTouches",false);view->setProperty("notifyJobs",true);view->setProperty("showDeviceFrame",true);
        view->setProperty("settingsTransfer",QString());view->setProperty("settingsTransferFailed",false);
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void main_screens_name_every_control_and_keep_a_keyboard_order() {
        // With assistive technology on, every control the keyboard reaches on the main screens has
        // a name, icon buttons included; Tab walks title bar, rail and device bar in that order with
        // a visible ring; the jobs list works with the keyboard.
        QAccessible::setActive(true);
        open("runtime");window->resize(1380,970);QTest::qWait(50);
        const double now=QDateTime::currentSecsSinceEpoch();
        const QVariantMap first{{"id","a11y-running"},{"label","Build Android · debug"},{"state","running"},{"created",now-30},{"updated",now}};
        const QVariantMap second{{"id","a11y-failed"},{"label","Lint"},{"state","failed"},{"created",now-300},{"updated",now-280}};
        workspace->setProperty("jobs",QVariantList{first,second});
        // The workspace's own job.list replies would replace the jobs shown; they are let through first.
        const auto showJobs=[&]{
            QTRY_VERIFY_WITH_TIMEOUT(mapOf(workspace->property("pending")).isEmpty(),patient(10000));
            workspace->setProperty("jobs",QVariantList{first,second});QTest::qWait(60);
        };
        for(const auto *tab:{"runtime","project","devices","jobs","tools","files","debug","testing","profiling","release"}) {
            open(QString::fromLatin1(tab));showJobs();
            int checked=0;
            std::function<void(QQuickItem *)> walk=[&](QQuickItem *it){
                if(!it->isVisible())return;
                const bool focusable=it->activeFocusOnTab() || (it->property("focusPolicy").isValid() && (it->property("focusPolicy").toInt()&Qt::TabFocus));
                const bool iconButton=it->property("iconOnly").toBool();
                if(focusable || iconButton) {
                    ++checked;
                    QVERIFY2(!accessibleName(it).trimmed().isEmpty(),qPrintable(QString("%1: %2 \"%3\" has no accessible name").arg(tab,QString::fromLatin1(it->metaObject()->className()),it->objectName())));
                }
                for(auto *child:it->childItems())walk(child);
            };
            walk(window->contentItem());
            if(QTest::currentTestFailed())break;
            QVERIFY2(checked>5,tab);
        }
        // Icon buttons are named by their label; the tip, with its shortcut, describes them.
        open("runtime");showJobs();
        auto *settings=item("settingsButton");QVERIFY(settings);
        QCOMPARE(accessibleName(settings),QString("Settings"));
        QCOMPARE(QAccessible::queryAccessibleInterface(settings)->text(QAccessible::Description),QString("Appearance and language · Ctrl+,"));
        QCOMPARE(accessibleName(item("nav-jobs")),QString("Builds & jobs"));
        // The keyboard order.
        item("projectButton")->forceActiveFocus(Qt::TabFocusReason);
        QStringList order;
        for(int i=0;i<60;++i) {
            QTest::keyClick(window,Qt::Key_Tab);
            auto *focused=window->activeFocusItem();
            const auto name=focused?focused->objectName():QString();
            if(name=="projectButton")break;
            order<<name;
            if(name=="settingsButton") {
                auto *ring=focused->property("background").value<QQuickItem *>();QVERIFY(ring);
                QCOMPARE(QQmlProperty(ring,"border.width").read().toInt(),2);
            }
        }
        QStringList wanted{"commandButton","activityButton","settingsButton","aboutButton","nav-runtime","nav-files","nav-jobs","nav-devices","nav-project","nav-debug","nav-testing","nav-profiling","nav-release","nav-tools","targetPicker"};
        int at=-1;
        for(const auto &name:wanted){const int next=order.indexOf(name);QVERIFY2(next>at,qPrintable(name+" out of order in "+order.join(", ")));at=next;}
        // Without focus the ring is gone.
        settings->setFocus(false);
        QCOMPARE(QQmlProperty(settings->property("background").value<QQuickItem *>(),"border.width").read().toInt(),1);
        // The jobs list: Tab into it, arrows move, Enter opens.
        open("jobs");showJobs();
        auto *list=item("jobList");QVERIFY(list);QTRY_COMPARE(list->property("count").toInt(),2);
        QVERIFY(list->activeFocusOnTab());
        list->forceActiveFocus(Qt::TabFocusReason);QTRY_VERIFY(list->hasActiveFocus());
        QTest::keyClick(window,Qt::Key_Down);QTest::keyClick(window,Qt::Key_Return);
        QTRY_COMPARE(workspace->property("selectedJob").toString(),QString("a11y-failed"));
        QCOMPARE(accessibleName(item("jobRow-a11y-failed")).left(14),QString("Lint, Failed, "));
        QAccessible::setActive(false);
        workspace->setProperty("selectedJob",QString());workspace->setProperty("activeJob",QVariantMap{});workspace->setProperty("jobs",QVariantList{});
        QVERIFY2(qmlErrors.isEmpty(),qPrintable(qmlErrors.join('\n')));
    }
    void cleanupTestCase() {
        engine.reset();control.reset();backend.reset();
    }
};

int main(int argc,char **argv) {
    // The checks measure windows on the offscreen test screen; run bare from a desktop session
    // they would land on the compositor, which sizes, tiles and focuses them itself. The same
    // environment as ctest's (CMakeLists.txt); -platform still picks a real display to watch.
    for(const auto &[key,value]:{std::pair{"QT_QPA_PLATFORM","offscreen"},{"QT_QUICK_BACKEND","software"},{"QT_QUICK_CONTROLS_STYLE","Basic"},{"QT_QPA_PLATFORMTHEME","generic"},{"QT_FORCE_STDERR_LOGGING","1"}})qputenv(key,value);
    QtWebEngineQuick::initialize();QGuiApplication app(argc,argv);
    // The window shows the product by the application name, so captures read as the real app.
    app.setOrganizationName("tomacco-ui-tests");app.setApplicationName("Tomacco");app.setApplicationVersion("9.8.7-test");
    qmlRegisterType<NativeView>("Tomacco.Native",1,0,"NativeView");
    DesktopUiTest test;return QTest::qExec(&test,argc,argv);
}
#include "desktop_ui_test.moc"
