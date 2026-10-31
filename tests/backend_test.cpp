#include "backend.h"
#include <QTest>
#include <QTemporaryDir>
#include <QFile>
#include <QDir>
#include <QTcpSocket>
#include <QEventLoop>
#include <QTimer>
#include <QSettings>
#include <QFileInfo>
#include <QCoreApplication>
#include <QProcess>
#include <QSignalSpy>
#include <QJsonDocument>
#include <QJsonObject>
#ifdef TOMACCO_HAVE_DBUS
#include <QDBusConnection>
#include <QDBusMessage>

// A stand-in for the desktop's notification service on a private bus.
class FakeNotifications: public QObject {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface","org.freedesktop.Notifications")
public:
    QVariantList last;
public slots:
    uint Notify(const QString &app,uint replaces,const QString &icon,const QString &summary,const QString &body,const QStringList &actions,const QVariantMap &hints,int timeout) {
        last={app,replaces,icon,summary,body,actions,hints.value("desktop-entry"),timeout};
        return 7;
    }
};
#endif

class BackendTest: public QObject {
    QTemporaryDir isolatedData;
    Q_OBJECT
    static void write(const QString &path,const QByteArray &data) {
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly)); file.write(data);
    }
    static QByteArray request(const QUrl &url,const QByteArray &target,const QByteArray &method="GET",const QByteArray &host={}) {
        QTcpSocket socket; QEventLoop loop;
        QObject::connect(&socket,&QTcpSocket::disconnected,&loop,&QEventLoop::quit);
        socket.connectToHost("127.0.0.1",url.port());
        if(!socket.waitForConnected(2000))return {};
        const auto actualHost=host.isEmpty()?QByteArray("127.0.0.1:")+QByteArray::number(url.port()):host;
        socket.write(method+" "+target+" HTTP/1.1\r\nHost: "+actualHost+"\r\nConnection: close\r\n\r\n");
        QTimer::singleShot(3000,&loop,&QEventLoop::quit);loop.exec();
        return socket.readAll();
    }
private slots:
    void initTestCase() {
        // Never the real data directory.
        QVERIFY(isolatedData.isValid());
        qputenv("TOMACCO_HOME",isolatedData.path().toUtf8());
        // Nor the real settings.
        qputenv("XDG_CONFIG_HOME",(isolatedData.path()+"/config").toUtf8());
    }
    void droppedFoldersOpenAsProjectsButFilesAndAppsDoNot() {
        QTemporaryDir root; QVERIFY(root.isValid());
        QVERIFY(QDir(root.path()).mkpath("app"));
        QVERIFY(QDir(root.path()).mkpath("Sample.app"));
        write(root.filePath("notes.txt"),"notes");
        Backend backend(QString(),nullptr,false);
        QCOMPARE(backend.projectFolder(root.filePath("app")),QFileInfo(root.filePath("app")).canonicalFilePath());
        QCOMPARE(backend.projectFolder(root.filePath("app")+"/"),QFileInfo(root.filePath("app")).canonicalFilePath());
        QVERIFY(backend.projectFolder(root.filePath("Sample.app")).isEmpty());
        QVERIFY(backend.projectFolder(root.filePath("notes.txt")).isEmpty());
        QVERIFY(backend.projectFolder(root.filePath("missing")).isEmpty());
        QVERIFY(backend.projectFolder(QString()).isEmpty());
    }
    void onlyJobLogFilesOpenAsJobLogs() {
        QTemporaryDir root; QVERIFY(root.isValid());
        QVERIFY(QDir(root.path()).mkpath("jobs"));
        const auto id=QString(32,'a');
        const auto log=root.filePath("jobs/"+id+".log");
        write(log,"Building\n");
        write(root.filePath("jobs/notes.log"),"x");
        write(root.filePath(id+".log"),"x");
        QVERIFY(QFile::link(log,root.filePath("jobs/"+QString(32,'b')+".log")));
        Backend backend(QString(),nullptr,false);
        QCOMPARE(backend.jobLogPath(log),QFileInfo(log).canonicalFilePath());
        QVERIFY(backend.jobLogPath(root.filePath("jobs/notes.log")).isEmpty());
        QVERIFY(backend.jobLogPath(root.filePath(id+".log")).isEmpty());
        QVERIFY(backend.jobLogPath(root.filePath("jobs/"+QString(32,'b')+".log")).isEmpty());
        QVERIFY(backend.jobLogPath(root.filePath("jobs/"+QString(32,'c')+".log")).isEmpty());
        QVERIFY(backend.jobLogPath(QString()).isEmpty());
    }
    void notificationsReachTheServiceAndComeBackWhenClicked() {
#ifndef TOMACCO_HAVE_DBUS
        QSKIP("Built without Qt D-Bus: the desktop sends no notifications");
#else
        // Never the user's own session bus: a private one, with a stand-in service.
        QProcess daemon;
        daemon.start("dbus-daemon",{"--session","--nofork","--print-address"});
        if(!daemon.waitForStarted(3000))QSKIP("dbus-daemon is not installed");
        QVERIFY(daemon.waitForReadyRead(5000));
        const auto address=QString::fromUtf8(daemon.readLine()).trimmed();
        QVERIFY(!address.isEmpty());
        qputenv("DBUS_SESSION_BUS_ADDRESS",address.toUtf8());
        {
            auto service=QDBusConnection::connectToBus(address,"fake-notifications");
            QVERIFY(service.isConnected());
            FakeNotifications fake;
            QVERIFY(service.registerObject("/org/freedesktop/Notifications",&fake,QDBusConnection::ExportAllSlots));
            QVERIFY(service.registerService("org.freedesktop.Notifications"));
            Backend backend(QString(),nullptr,false);
            QSignalSpy sent(&backend,&Backend::notificationSent),clicked(&backend,&Backend::notificationActivated);
            QVERIFY(backend.notify("Build Android","Completed after 1 min 2 s","job-1"));
            QCOMPARE(sent.size(),1);
            QTRY_COMPARE(fake.last.value(3).toString(),QString("Build Android"));
            QCOMPARE(fake.last.value(4).toString(),QString("Completed after 1 min 2 s"));
            QCOMPARE(fake.last.value(5).toStringList().value(0),QString("default"));
            QCOMPARE(fake.last.value(6).toString(),QString("io.github.marselnenaj.tomacco"));
            // The service names the notification it showed; a click on it comes back with the tag.
            QTest::qWait(200);
            auto signal=QDBusMessage::createSignal("/org/freedesktop/Notifications","org.freedesktop.Notifications","ActionInvoked");
            signal<<uint(7)<<QString("default");
            QVERIFY(service.send(signal));
            QTRY_COMPARE(clicked.size(),1);
            QCOMPARE(clicked.at(0).at(0).toString(),QString("job-1"));
            // Another notification's click, or a second one, is not ours.
            auto other=QDBusMessage::createSignal("/org/freedesktop/Notifications","org.freedesktop.Notifications","ActionInvoked");
            other<<uint(7)<<QString("default");
            QVERIFY(service.send(other));
            QTest::qWait(200);
            QCOMPARE(clicked.size(),1);
        }
        QDBusConnection::disconnectFromBus("fake-notifications");
        daemon.terminate();daemon.waitForFinished(3000);
#endif
    }
    void settingsMoveToAnotherMachineWithoutWhatBelongsToThisOne() {
        QTemporaryDir root; QVERIFY(root.isValid());
        QVERIFY(QSettings().fileName().startsWith(isolatedData.path()));
        Backend backend(QString(),nullptr,false);
        backend.setAppearance("dark");
        backend.saveSetting("Notifications/jobs",false);backend.saveSetting("NativeDisplay/showTouches",true);
        backend.saveSetting("NativeIphone/runnerBundle","com.example.runner");backend.saveSetting("device","pixel-11");
        // This machine's: never exported.
        backend.saveSetting("window/width",1200);backend.saveSetting("recentProjects",QStringList{"/home/someone/app"});
        backend.saveSetting("jobs/origins","{}");backend.saveSetting("IosResearch/engine","/opt/engine");backend.saveSetting("lastUrl","http://localhost:5173");
        const auto path=root.filePath("settings.json");
        QCOMPARE(backend.exportSettings(path,{{"updates/mode","auto"},{"updates/channel","prerelease"},{"token","secret"}}),QString());
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));
        const auto text=file.readAll();file.close();
        const auto document=QJsonDocument::fromJson(text).object();
        QCOMPARE(document.value("format").toString(),QString("tomacco-desktop-settings"));QCOMPARE(document.value("version").toInt(),1);
        const auto values=document.value("settings").toObject();
        QCOMPARE(values.value("appearance").toString(),QString("dark"));
        QCOMPARE(values.value("Notifications/jobs"),QJsonValue(false));QCOMPARE(values.value("NativeDisplay/showTouches"),QJsonValue(true));
        QCOMPARE(values.value("NativeIphone/runnerBundle").toString(),QString("com.example.runner"));
        QCOMPARE(values.value("device").toString(),QString("pixel-11"));QCOMPARE(values.value("updates/mode").toString(),QString("auto"));
        QCOMPARE(values.value("updates/channel").toString(),QString("prerelease"));
        for(const auto *banned:{"window/width","recentProjects","jobs/origins","IosResearch/engine","lastUrl","token","secret","/home/someone","/opt/engine"})
            QVERIFY2(!text.contains(banned),banned);
        // On the other machine: each value is checked, the rest ignored.
        QSettings().clear();
        QJsonObject edited=values;
        edited.insert("appearance","light");edited.insert("NativeDisplay/showActivity","yes");edited.insert("device","../../etc");
        edited.insert("window/width",900);edited.insert("language","klingon");
        write(path,QJsonDocument(QJsonObject{{"format","tomacco-desktop-settings"},{"version",1},{"settings",edited}}).toJson());
        QSignalSpy appearance(&backend,&Backend::appearanceChanged);
        const auto result=backend.importSettings(path);
        QVERIFY(result.value("ok").toBool());
        QCOMPARE(result.value("applied").toInt(),6);QCOMPARE(result.value("ignored").toInt(),4);
        QCOMPARE(result.value("extra").toMap().value("updates/mode").toString(),QString("auto"));
        QCOMPARE(result.value("extra").toMap().value("updates/channel").toString(),QString("prerelease"));
        QCOMPARE(backend.appearance(),QString("light"));QCOMPARE(appearance.size(),1);
        QCOMPARE(backend.setting("Notifications/jobs").toBool(),false);QCOMPARE(backend.setting("NativeDisplay/showTouches").toBool(),true);
        QCOMPARE(backend.setting("NativeIphone/runnerBundle").toString(),QString("com.example.runner"));
        QVERIFY(!QSettings().contains("NativeDisplay/showActivity"));QVERIFY(!QSettings().contains("device"));
        QVERIFY(!QSettings().contains("window/width"));QVERIFY(!QSettings().contains("language"));QVERIFY(!QSettings().contains("updates/mode"));
        // Files that are no settings file, or from a newer version, change nothing.
        write(path,"not json");QVERIFY(!backend.importSettings(path).value("ok").toBool());
        write(path,R"({"format":"something-else","version":1,"settings":{"appearance":"dark"}})");QVERIFY(!backend.importSettings(path).value("ok").toBool());
        write(path,R"({"format":"tomacco-desktop-settings","version":2,"settings":{"appearance":"dark"}})");
        QVERIFY(backend.importSettings(path).value("error").toString().contains("newer"));
        QVERIFY(!backend.importSettings(root.filePath("missing.json")).value("ok").toBool());
        QCOMPARE(backend.appearance(),QString("light"));
        QVERIFY(!backend.exportSettings(root.filePath("no/such/dir/settings.json")).isEmpty());
        QSettings().clear();
    }
    void aRestartGoesThroughTheInstalledLauncherWithThisProjectAndView() {
        QTemporaryDir home;QVERIFY(home.isValid());
        const auto library=home.filePath(".local/lib/tomacco/tomacco"),launcher=home.filePath(".local/bin/tomacco");
        // Without a launcher, or with someone else's, the program itself.
        QCOMPARE(Backend::relaunchProgram(library+" (deleted)",home.path()),library);
        QVERIFY(QDir().mkpath(QFileInfo(launcher).absolutePath()));
        QFile script(launcher);QVERIFY(script.open(QIODevice::WriteOnly));script.write("#!/bin/sh\nexec /elsewhere/tomacco \"$@\"\n");script.close();
        QVERIFY(script.setPermissions(QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner));
        QCOMPARE(Backend::relaunchProgram(library,home.path()),library);
        // install.py's launcher sets the environment the program needs.
        QVERIFY(script.open(QIODevice::WriteOnly));script.write("#!/bin/sh\n# Managed by tomacco/install.py\nexec "+library.toUtf8()+" \"$@\"\n");script.close();
        QCOMPARE(Backend::relaunchProgram(library+" (deleted)",home.path()),launcher);
        // A build or a package elsewhere starts itself.
        QCOMPARE(Backend::relaunchProgram("/opt/build/tomacco",home.path()),QString("/opt/build/tomacco"));
        QCOMPARE(Backend::relaunchArguments({"--project","/old","--workspace=jobs","--preview-session","work","--capture","/tmp/x.png","--smoke-test","--device-only"},"/new","files"),
            (QStringList{"--preview-session","work","--device-only","--project","/new","--workspace","files"}));
        QCOMPARE(Backend::relaunchArguments({"--project=/old"},"",""),QStringList{});
    }
    void theWhatsNewSheetReadsThisVersionsHighlights() {
        Backend backend(QString(),nullptr,false);
        const auto path=backend.documentPath("RELEASE_NOTES.md");
        if(path.isEmpty())QSKIP("The release notes are not beside this build");
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));
        const auto version=QString::fromUtf8(file.readLine()).trimmed().mid(QStringLiteral("# Tomacco ").size());
        const auto highlights=backend.releaseHighlights(version);
        QVERIFY(highlights.startsWith("- "));
        QVERIFY(!highlights.contains("## "));QVERIFY(!highlights.contains("]("));
        QCOMPARE(backend.releaseHighlights("0.0.1"),QString());QCOMPARE(backend.releaseHighlights(""),QString());
    }
    void dataDirectoryIsTheVariableOrTheDefault() {
        QTemporaryDir home;
        QVERIFY(home.isValid());
        const auto savedHome=qgetenv("HOME"), savedData=qgetenv("TOMACCO_HOME");
        qputenv("HOME",home.path().toUtf8());
        qunsetenv("TOMACCO_HOME");
        const auto current=home.filePath(".local/share/tomacco");
        // A fresh installation: nothing is created.
        QCOMPARE(Backend::dataDirectory(),current);
        QVERIFY(!QFileInfo::exists(current));
        qputenv("TOMACCO_HOME","/new/place");
        QCOMPARE(Backend::dataDirectory(),QString("/new/place"));
        qputenv("TOMACCO_HOME",savedData);
        qputenv("HOME",savedHome);
    }
    void addresses() {
        Backend backend("",nullptr,false);
        QCOMPARE(backend.normalizeUrl("localhost:5173").toString(),"http://localhost:5173");
        QCOMPARE(backend.normalizeUrl("example.com").toString(),"https://example.com");
        QCOMPARE(backend.normalizeUrl("tomacco://demo"),backend.demoUrl());
        QVERIFY(backend.normalizeUrl("file:///etc/passwd").isEmpty());
        QVERIFY(!backend.allowedUrl(QUrl("javascript:alert(1)")));
        QVERIFY(!backend.allowedUrl(QUrl("qrc:/src/backend.cpp")));
    }
    void localAssetsAndBoundaries() {
        QTemporaryDir temp; QVERIFY(temp.isValid());
        QDir dir(temp.path()); dir.mkdir("public");
        const auto root=temp.path()+"/public";
        write(root+"/index.html","<h1>Preview fixture</h1>");
        write(root+"/app.js","window.fixture=true;");
        write(root+"/.env","TOKEN=private");
        write(temp.path()+"/outside.txt","Outside root");
        QVERIFY(QFile::link(temp.path()+"/outside.txt",root+"/escape.txt"));
        Backend backend("",nullptr,false);
        auto url=backend.serveFolder(QUrl::fromLocalFile(root)); QVERIFY(!url.isEmpty());
        auto html=request(url,"/"); QVERIFY(html.startsWith("HTTP/1.1 200")); QVERIFY(html.endsWith("<h1>Preview fixture</h1>"));
        auto script=request(url,"/app.js"); QVERIFY(script.startsWith("HTTP/1.1 200")); QVERIFY(script.contains("javascript"));
        QVERIFY(request(url,"/dashboard/settings").endsWith("<h1>Preview fixture</h1>"));
        QVERIFY(request(url,"/missing.png").startsWith("HTTP/1.1 404"));
        QVERIFY(request(url,"/%2e%2e/outside.txt").startsWith("HTTP/1.1 403"));
        QVERIFY(request(url,"/escape.txt").startsWith("HTTP/1.1 404"));
        QVERIFY(request(url,"/.env").startsWith("HTTP/1.1 403"));
        // A visible name inside the folder that resolves to a hidden file.
        QVERIFY(QFile::link(root+"/.env",root+"/settings.txt"));
        QVERIFY(request(url,"/settings.txt").startsWith("HTTP/1.1 403"));
        QVERIFY(request(url,"/","POST").startsWith("HTTP/1.1 405"));
        QVERIFY(request(url,"/","GET","attacker.example").startsWith("HTTP/1.1 403"));
        const auto head=request(url,"/","HEAD"); QVERIFY(head.startsWith("HTTP/1.1 200")); QVERIFY(head.endsWith("\r\n\r\n"));
        QVERIFY(backend.serveFolder(QUrl::fromLocalFile(temp.path()+"/missing")).isEmpty());
        QVERIFY(request(url,"/").startsWith("HTTP/1.1 200"));
    }
};
QTEST_GUILESS_MAIN(BackendTest)
#include "backend_test.moc"
