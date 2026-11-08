#include "backend.h"
#include <QTest>
#include <QRegularExpression>
#include <QUrl>
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
#include <QScopeGuard>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocale>
#include <QCollator>
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

// A stand-in file manager on a private bus.
class FakeFileManager: public QObject {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface","org.freedesktop.FileManager1")
public:
    QStringList shown;
public slots:
    void ShowItems(const QStringList &uris,const QString &) { shown=uris; }
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
    void theHyprlandClientIsThisProcesssWindowWithThatTitle() {
        const QByteArray clients=R"([
            {"address":"0x1","pid":42,"title":"Tomacco","floating":false,"pinned":false},
            {"address":"0x2","pid":7,"title":"Tomacco – iPhone Air","floating":true,"pinned":true},
            {"address":"0x3","pid":42,"title":"Tomacco – iPhone Air","floating":true,"pinned":false}])";
        const auto mine=Backend::hyprlandClient(clients,42,QStringLiteral("Tomacco – iPhone Air"));
        QCOMPARE(mine.value("address").toString(),QStringLiteral("0x3"));
        QCOMPARE(mine.value("floating").toBool(),true);
        QCOMPARE(mine.value("pinned").toBool(),false);
        QVERIFY(Backend::hyprlandClient(clients,42,QStringLiteral("Other")).value("address").toString().isEmpty());
        QVERIFY(Backend::hyprlandClient("not json",42,QStringLiteral("Tomacco")).value("address").toString().isEmpty());
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
    void theProjectChooserListsFoldersAndResolvesTypedPaths() {
        QTemporaryDir root; QVERIFY(root.isValid());
        const QString base=root.filePath(QString::fromUtf8("Projekte mit Leerzeichen · 日本語"));
        for(const auto &name:{QStringLiteral("app10"),QStringLiteral("App9"),QStringLiteral("app1"),QString::fromUtf8("Überlänge العربية"),QStringLiteral(".hidden")})QVERIFY(QDir().mkpath(base+"/"+name));
        write(base+"/notes.txt","a file");
        QVERIFY(QFile::link(base+"/app1",base+"/link to app1"));
        QVERIFY(QFile::link(base+"/gone",base+"/broken link"));
        Backend backend(QString(),nullptr,false);
        // Typed and pasted forms of one folder; symbolic links stay in the path.
        QCOMPARE(backend.resolveFolderPath(base+"/"),base);
        QCOMPARE(backend.resolveFolderPath("  "+base+"/app1/../app10\n"),base+"/app10");
        QCOMPARE(backend.resolveFolderPath(QUrl::fromLocalFile(base).toString(QUrl::FullyEncoded)),base);
        QCOMPARE(backend.resolveFolderPath("app9",base),base+"/app9");
        QCOMPARE(backend.resolveFolderPath(QString(),base),base);
        QCOMPARE(backend.resolveFolderPath("~"),QDir::homePath());
        QCOMPARE(backend.resolveFolderPath("~/x"),QDir::cleanPath(QDir::homePath()+"/x"));
        QCOMPARE(backend.resolveFolderPath(base+"/link to app1"),base+"/link to app1");
        QVERIFY(backend.resolveFolderPath("https://example.invalid/a").startsWith(QDir::homePath()));
        // Folders only, in reading order (numbers as numbers, case ignored); hidden ones when asked;
        // a link names its target, a broken link and a file are left out.
        auto listing=backend.folderListing(base);
        QCOMPARE(listing.value("path").toString(),base);
        QCOMPARE(listing.value("parent").toString(),QDir::cleanPath(root.path()));
        QCOMPARE(listing.value("error").toString(),QString());
        QStringList names;QVariantMap link;
        for(const auto &entry:listing.value("folders").toList()){const auto folder=entry.toMap();names<<folder.value("name").toString();if(folder.value("link").toBool())link=folder;
            QCOMPARE(folder.value("path").toString(),base+"/"+folder.value("name").toString());}
        QCOMPARE(names,(QStringList{"app1","App9","app10","link to app1",QString::fromUtf8("Überlänge العربية")}));
        QCOMPARE(link.value("target").toString(),QFileInfo(base+"/app1").canonicalFilePath());
        QVERIFY(backend.folderListing(base,{},true).value("folders").toList().size()==6);
        // What cannot be listed says why.
        QCOMPARE(backend.folderListing(base+"/missing").value("error").toString(),QString("missing"));
        QCOMPARE(backend.folderListing(base+"/notes.txt").value("error").toString(),QString("not-folder"));
        QVERIFY(QFile::setPermissions(base+"/app10",QFile::WriteOwner));
        const auto restore=qScopeGuard([&]{QFile::setPermissions(base+"/app10",QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner);});
        if(!QFileInfo(base+"/app10").isReadable())QCOMPARE(backend.folderListing(base+"/app10").value("error").toString(),QString("unreadable"));
        QCOMPARE(backend.folderListing("/").value("parent").toString(),QString());
        // Choosing goes through projectFolder: a link opens its target, an app bundle is no project.
        QCOMPARE(backend.projectFolder(backend.resolveFolderPath("link to app1",base)),QFileInfo(base+"/app1").canonicalFilePath());
    }
    void theFileChooserListsFilesNamesSaveTargetsAndEncodesUrls() {
        QTemporaryDir root; QVERIFY(root.isValid());
        const QString base=root.filePath(QString::fromUtf8("Dateien #1 %20 · 日本語"));
        QVERIFY(QDir().mkpath(base+"/Ordner"));QVERIFY(QDir().mkpath(base+"/.versteckt"));
        for(const auto &name:{QStringLiteral("szene10.json"),QStringLiteral("Szene9.JSON"),QStringLiteral("notes.txt"),QStringLiteral(".hidden.json"),QString::fromUtf8("مشهد.json"),QStringLiteral("video.MP4")})write(base+"/"+name,"{}");
        QVERIFY(QFile::link(base+"/szene10.json",base+"/link.json"));
        QVERIFY(QFile::link(base+"/gone.json",base+"/broken.json"));
        Backend backend(QString(),nullptr,false);
        // Wildcards as Qt's dialogs read a name filter.
        QCOMPARE(Backend::filterPatterns("Videos (*.mp4 *.mov *.mkv *.webm)"),(QStringList{"*.mp4","*.mov","*.mkv","*.webm"}));
        QCOMPARE(Backend::filterPatterns("All files (*)"),QStringList{"*"});
        QCOMPARE(Backend::filterPatterns("*.json"),QStringList{"*.json"});
        QCOMPARE(Backend::filterPatterns(QString::fromUtf8("Einstellungen (Tomacco) (*.json)")),QStringList{"*.json"});
        // Files of the type, case ignored, in reading order, links included, broken links and hidden ones not.
        const auto names=[](const QVariantList &list){QStringList out;for(const auto &e:list)out<<e.toMap().value("name").toString();return out;};
        auto listing=backend.fileListing(base,{},false,{"*.json"});
        QCOMPARE(names(listing.value("folders").toList()),QStringList{"Ordner"});
        QCOMPARE(names(listing.value("files").toList()),(QStringList{"link.json","Szene9.JSON","szene10.json",QString::fromUtf8("مشهد.json")}));
        for(const auto &e:listing.value("files").toList()){const auto file=e.toMap();QCOMPARE(file.value("path").toString(),base+"/"+file.value("name").toString());
            QCOMPARE(file.value("link").toBool(),file.value("name").toString()=="link.json");
            if(file.value("link").toBool())QCOMPARE(file.value("target").toString(),QFileInfo(base+"/szene10.json").canonicalFilePath());}
        QCOMPARE(names(backend.fileListing(base,{},false,{"*"}).value("files").toList()).size(),6);
        QCOMPARE(names(backend.fileListing(base,{},false,{}).value("files").toList()).size(),6);
        QCOMPARE(names(backend.fileListing(base,{},true,{"*.json"}).value("files").toList()).size(),5);
        QCOMPARE(names(backend.fileListing(base,{},true,{"*.json"}).value("folders").toList()).size(),2);
        QCOMPARE(names(backend.fileListing(base,{},false,Backend::filterPatterns("Videos (*.mp4 *.mov)")).value("files").toList()),QStringList{"video.MP4"});
        QCOMPARE(backend.fileListing(base+"/fehlt",{},false,{}).value("error").toString(),QString("missing"));
        QVERIFY(backend.fileListing(base+"/fehlt",{},false,{}).value("files").toList().isEmpty());
        // The folder listing of the project chooser is unchanged: no files key.
        QVERIFY(!backend.folderListing(base).contains("files"));
        // What a path is.
        QVERIFY(backend.pathInfo(base+"/szene10.json").value("file").toBool());
        QVERIFY(backend.pathInfo(base+"/Ordner").value("folder").toBool());
        QVERIFY(backend.pathInfo(base+"/link.json").value("link").toBool() && backend.pathInfo(base+"/link.json").value("file").toBool());
        QVERIFY(backend.pathInfo(base+"/broken.json").value("exists").toBool() && !backend.pathInfo(base+"/broken.json").value("file").toBool());
        QVERIFY(!backend.pathInfo(base+"/fehlt.json").value("exists").toBool());
        QVERIFY(!backend.pathInfo(QString()).value("exists").toBool());
        // Save targets: the default suffix only on a name without a dot; names neither trimmed nor resolved.
        QCOMPARE(backend.saveTarget("neu",base,"json"),base+"/neu.json");
        QCOMPARE(backend.saveTarget("neu",base,".json"),base+"/neu.json");
        QCOMPARE(backend.saveTarget("neu.v2",base,"json"),base+"/neu.v2");
        QCOMPARE(backend.saveTarget(" neu ",base,"json"),base+"/ neu .json");
        QCOMPARE(backend.saveTarget("neu",base,{}),base+"/neu");
        QCOMPARE(backend.saveTarget("Ordner",base,"json"),base+"/Ordner");
        QCOMPARE(backend.saveTarget("Ordner/",base,"json"),base+"/Ordner");
        QCOMPARE(backend.saveTarget("..",base,"json"),QDir::cleanPath(root.path()));
        QCOMPARE(backend.saveTarget("Ordner/innen",base,"json"),base+"/Ordner/innen.json");
        QCOMPARE(backend.saveTarget(base+"/abs","/elsewhere","json"),base+"/abs.json");
        QCOMPARE(backend.saveTarget("~/x",base,"json"),QDir::cleanPath(QDir::homePath()+"/x.json"));
        QCOMPARE(backend.saveTarget(QUrl::fromLocalFile(base+"/url #1").toString(QUrl::FullyEncoded),base,"json"),base+"/url #1.json");
        QCOMPARE(backend.saveTarget("a\nb",base,"json"),base+"/ab.json");
        QCOMPARE(backend.saveTarget("   ",base,"json"),QString());
        QCOMPARE(backend.saveTarget("https://example.invalid/x",base,"json"),base+"/https:/example.invalid/x.json");
        // A path as a URL: # and % stay part of the name.
        const auto url=backend.fileUrl(base+"/a #1 100%.json");
        QVERIFY(url.isLocalFile());QCOMPARE(url.toLocalFile(),base+"/a #1 100%.json");
        QVERIFY(url.toString(QUrl::FullyEncoded).contains("%231%20100%25.json"));
        QCOMPARE(QUrl::fromPercentEncoding(url.toString().toUtf8()).mid(7),base+"/a #1 100%.json");
        QVERIFY(backend.fileUrl(QString()).isEmpty());
    }
    // The chooser's reading order in the C locale (LC_ALL=C, C.UTF-8, POSIX, as test pipelines and
    // minimal systems run) and in interface languages; QLocale's default is set for the row only.
    void theProjectChooserOrdersFoldersNaturallyInEveryLocale_data() {
        QTest::addColumn<QLocale>("locale");
        QTest::addColumn<QStringList>("order");
        const QString decomposed=QStringLiteral(u"U\u0308ber"),composed=QStringLiteral(u"\u00dcber"),
            mixed=QStringLiteral(u"Überlänge العربية"),apfel=QStringLiteral(u"Äpfel"),
            arabic=QStringLiteral(u"العربية"),file2=QStringLiteral(u"ملف٢"),
            file10=QStringLiteral(u"ملف١٠"),kana=QStringLiteral(u"かな"),kanji=QStringLiteral(u"日本語"),
            long2=QString(250,'x')+"2",long10=QString(250,'x')+"10";
        // Numbers as numbers, case ignored (a tie in the collation goes by code units: APP1 before app1,
        // the decomposed U+0308 spelling before the composed one); letters with marks beside their base letter.
        const QStringList latin{apfel,"APP1","app1","App9","app10","link to app1",decomposed,composed,mixed,long2,long10,"Zebra"};
        const QStringList english=latin+QStringList{arabic,file2,file10,kana,kanji};
        QTest::newRow("C")<<QLocale::c()<<english;
        QTest::newRow("C.UTF-8")<<QLocale(QStringLiteral("C.UTF-8"))<<english;
        QTest::newRow("POSIX")<<QLocale(QStringLiteral("POSIX"))<<english;
        QTest::newRow("en-US")<<QLocale(QStringLiteral("en-US"))<<english;
        QTest::newRow("de-DE")<<QLocale(QStringLiteral("de-DE"))<<english;
        // Swedish keeps its own alphabet (Ä after Z, Ü as Y): the language's order, not English for all.
        QTest::newRow("sv-SE")<<QLocale(QStringLiteral("sv-SE"))<<QStringList{"APP1","app1","App9","app10","link to app1",long2,long10,
            decomposed,composed,mixed,"Zebra",apfel,arabic,file2,file10,kana,kanji};
        // Arabic script first, Arabic-Indic digits as numbers.
        QTest::newRow("ar-AE")<<QLocale(QStringLiteral("ar-AE"))<<QStringList{arabic,file2,file10}+latin+QStringList{kana,kanji};
        QTest::newRow("ja-JP")<<QLocale(QStringLiteral("ja-JP"))<<latin+QStringList{kana,kanji,arabic,file2,file10};
    }
    void theProjectChooserOrdersFoldersNaturallyInEveryLocale() {
        QFETCH(QLocale,locale);QFETCH(QStringList,order);
        QTemporaryDir root; QVERIFY(root.isValid());
        const QLocale before=QLocale();
        {
            const auto restore=qScopeGuard([&]{QLocale::setDefault(before);});
            QLocale::setDefault(locale);
            // What the listing's collator starts from: the C rows are Qt's C collation, the others the language's.
            QCOMPARE(QCollator().locale().language(),locale.language());
            // The same folders made in two orders list the same: one total order, whatever the directory returns.
            QStringList created=order;std::reverse(created.begin(),created.end());
            for(int pass=0;pass<2;++pass) {
                const QStringList variant=pass?created:order;
                const QString base=root.filePath(QString::number(pass));
                for(const auto &name:variant) {
                    if(name==QLatin1String("link to app1")) continue;
                    QVERIFY2(QDir().mkpath(base+"/"+name),qPrintable(name));
                }
                QVERIFY(QFile::link(base+"/app1",base+"/link to app1"));
                Backend backend(QString(),nullptr,false);
                QStringList names;
                for(const auto &entry:backend.folderListing(base).value("folders").toList())names<<entry.toMap().value("name").toString();
                QCOMPARE(names,order);
            }
        }
        QCOMPARE(QLocale(),before);
        QCOMPARE(QCollator().locale(),before.collation());
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
    void anAgentsSessionLogIsReadOnwardLineByLine() {
        QTemporaryDir root; QVERIFY(root.isValid());
        const auto log=root.filePath("session.jsonl");
        Backend backend(QString(),nullptr,false);
        QVERIFY(backend.readSessionLog(log,0).value("error").toString().contains("No session log"));
        QVERIFY(!backend.readSessionLog("relative.jsonl",0).value("error").toString().isEmpty());
        QVERIFY(backend.readSessionLog(QString(),0).value("entries").toList().isEmpty());
        // Whole lines only; a line that is no JSON object is passed over.
        const QByteArray first=R"({"time":1,"operation":"device.list","ok":true,"duration_seconds":0.2})" "\n";
        write(log,first+"not json\n"+R"({"time":2,"operation":"project.run","ok":false)");
        auto read=backend.readSessionLog(log,0);
        QCOMPARE(read.value("error").toString(),QString());
        auto entries=read.value("entries").toList();
        QCOMPARE(entries.size(),1);
        QCOMPARE(entries.first().toMap().value("operation").toString(),QString("device.list"));
        const auto offset=read.value("next_offset").toLongLong();
        QCOMPARE(offset,qint64(first.size()+9));
        // The rest of the line arrives: read from where the last read stopped.
        {QFile file(log);QVERIFY(file.open(QIODevice::Append));file.write(R"(,"error":{"code":"no_device","message":"No device"}})" "\n");}
        read=backend.readSessionLog(log,offset);
        entries=read.value("entries").toList();
        QCOMPARE(entries.size(),1);
        QCOMPARE(entries.first().toMap().value("error").toMap().value("code").toString(),QString("no_device"));
        QVERIFY(!read.value("reset").toBool());
        QVERIFY(backend.readSessionLog(log,read.value("next_offset").toLongLong()).value("entries").toList().isEmpty());
        // A log started anew is read from its beginning.
        write(log,first);
        read=backend.readSessionLog(log,offset+200);
        QVERIFY(read.value("reset").toBool());
        QCOMPARE(read.value("entries").toList().size(),1);
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
            // A recording is shown selected in the file manager. The session
            // bus connection is made once per process, so this check shares
            // the bus of the notifications.
            FakeFileManager files;
            QVERIFY(service.registerObject("/org/freedesktop/FileManager1",&files,QDBusConnection::ExportAllSlots));
            QVERIFY(service.registerService("org.freedesktop.FileManager1"));
            QTemporaryDir root; QVERIFY(root.isValid());
            const auto video=root.filePath("clip.mp4");write(video,"mp4");
            QSignalSpy revealed(&backend,&Backend::fileRevealed);
            QVERIFY(backend.revealFile(video));
            QCOMPARE(revealed.size(),1);
            QTRY_COMPARE(files.shown,QStringList{QUrl::fromLocalFile(video).toString()});
        }
        QDBusConnection::disconnectFromBus("fake-notifications");
        daemon.terminate();daemon.waitForFinished(3000);
#endif
    }
    void recordingsGoToTheVideosFolderAndAreShownInTheFileManager() {
        QTemporaryDir root; QVERIFY(root.isValid());
        QVERIFY(QDir().mkpath(isolatedData.path()+"/config"));
        write(isolatedData.path()+"/config/user-dirs.dirs",("XDG_VIDEOS_DIR=\""+root.path()+"/videos\"\n").toUtf8());
        Backend backend(QString(),nullptr,false);
        const auto path=backend.recordingPath("Pixel 11 · Test");
        QVERIFY2(QRegularExpression("^"+QRegularExpression::escape(root.path())+"/videos/Tomacco/Pixel-11---Test-\\d{8}-\\d{6}\\.mp4$").match(path).hasMatch(),qPrintable(path));
        QVERIFY(QFileInfo(root.path()+"/videos/Tomacco").isDir());
        QFile::remove(isolatedData.path()+"/config/user-dirs.dirs");
        // One thumbnail file per device, whatever its serial holds.
        QCOMPARE(QFileInfo(backend.thumbnailPath("192.168.1.20:37099")).fileName(),QString("192.168.1.20-37099.png"));
        QCOMPARE(backend.thumbnailPath("emulator-5554"),backend.thumbnailPath("emulator-5554"));
        QVERIFY(!backend.revealFile(root.filePath("missing.mp4")));
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
    void interfaceScaleIsValidatedPersistedAndImportedLive() {
        QSettings settings;
        const auto previous=settings.value("appearance/uiScale");
        const auto restore=qScopeGuard([&]{if(previous.isValid())settings.setValue("appearance/uiScale",previous);else settings.remove("appearance/uiScale");});
        settings.remove("appearance/uiScale");
        Backend backend(QString(),nullptr,false);
        QCOMPARE(backend.uiScale(),100);
        for(const auto &bad:{"0","300","125.5","banana"}) {
            settings.setValue("appearance/uiScale",bad);
            QCOMPARE(backend.uiScale(),100);
        }
        QSignalSpy changed(&backend,&Backend::uiScaleChanged);
        backend.setUiScale(150);
        QCOMPARE(backend.uiScale(),150);QCOMPARE(changed.size(),1);
        backend.setUiScale(150);QCOMPARE(changed.size(),1);
        settings.sync();
        Backend reopened(QString(),nullptr,false);QCOMPARE(reopened.uiScale(),150);
        QTemporaryDir root;QVERIFY(root.isValid());
        const auto path=root.filePath("scale.json");
        QCOMPARE(reopened.exportSettings(path),QString());
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));
        QCOMPARE(QJsonDocument::fromJson(file.readAll()).object().value("settings").toObject().value("appearance/uiScale"),QJsonValue(150));file.close();
        for(const auto &bad:{QJsonValue("200"),QJsonValue(true),QJsonValue(150.5),QJsonValue(300)}) {
            write(path,QJsonDocument(QJsonObject{{"format","tomacco-desktop-settings"},{"version",1},{"settings",QJsonObject{{"appearance/uiScale",bad}}}}).toJson());
            const auto result=backend.importSettings(path);QCOMPARE(result.value("ignored").toInt(),1);QCOMPARE(backend.uiScale(),150);
        }
        write(path,R"({"format":"tomacco-desktop-settings","version":1,"settings":{"appearance/uiScale":200}})");
        QCOMPARE(backend.importSettings(path).value("applied").toInt(),1);
        QCOMPARE(backend.uiScale(),200);QCOMPARE(changed.size(),2);
        backend.setUiScale(-100);QCOMPARE(backend.uiScale(),75);
        backend.setUiScale(100000);QCOMPARE(backend.uiScale(),200);
    }
    void screenshotsGoWhereOmarchyKeepsThem() {
        const auto home=QDir::homePath();
        // OMARCHY_SCREENSHOT_DIR wins, with ~ and $HOME read as the shell reads them.
        QCOMPARE(Backend::screenshotDirectoryFor("/mnt/shots","/p",true),QString("/mnt/shots"));
        QCOMPARE(Backend::screenshotDirectoryFor("$HOME/Pictures/Screenshots/","/p",false),home+"/Pictures/Screenshots");
        QCOMPARE(Backend::screenshotDirectoryFor("~/Shots","/p",true),home+"/Shots");
        QCOMPARE(Backend::screenshotDirectoryFor("  ","/p",true),QString("/p"));
        // Without it, Omarchy's own screenshots sit in Pictures; elsewhere Tomacco keeps a folder.
        QCOMPARE(Backend::screenshotDirectoryFor({},"/p",true),QString("/p"));
        QCOMPARE(Backend::screenshotDirectoryFor({},"/p",false),QString("/p/Tomacco"));
    }
    void theActiveOmarchyThemeIsReadNotWritten() {
        // Omarchy's colors.toml: quoted colours, comments, a mode; anything else is left out.
        const auto colours=Backend::parseOmarchyColors(
            "# Nenaj\nmode = \"dark\"\naccent = \"#D8FF3D\" # lime\nbackground='#0a0a0a'\nforeground = \"#f2f0ea\"\n"
            "red = \"red\"\nmuted = \"#123\"\n[section]\nselection = #38420f\n");
        QCOMPARE(colours.value("mode").toString(),QString("dark"));
        QCOMPARE(colours.value("accent").toString(),QString("#d8ff3d"));
        QCOMPARE(colours.value("background").toString(),QString("#0a0a0a"));
        QCOMPARE(colours.value("foreground").toString(),QString("#f2f0ea"));
        for(const auto *left:{"red","muted","selection"})QVERIFY2(!colours.contains(left),left);
        QVERIFY(Backend::parseOmarchyColors("mode = \"sepia\"").isEmpty());
        // The active theme: XDG_STATE_HOME's omarchy/current/theme, then the configuration's, as older releases keep it.
        QTemporaryDir root;QVERIFY(root.isValid());
        const auto state=root.filePath("state"),config=root.filePath("config");
        const auto oldState=qgetenv("XDG_STATE_HOME"),oldConfig=qgetenv("XDG_CONFIG_HOME");
        auto restore=qScopeGuard([&]{qputenv("XDG_STATE_HOME",oldState);qputenv("XDG_CONFIG_HOME",oldConfig);});
        qputenv("XDG_STATE_HOME",state.toUtf8());qputenv("XDG_CONFIG_HOME",config.toUtf8());
        QCOMPARE(Backend::omarchyThemeDirectory(),QString());
        QVERIFY(QDir().mkpath(config+"/omarchy/current/theme"));
        write(config+"/omarchy/current/theme/colors.toml","accent = \"#4fb3ff\"\nbackground = \"#101418\"\n");
        QCOMPARE(Backend::omarchyThemeDirectory(),config+"/omarchy/current/theme");
        QVERIFY(QDir().mkpath(state+"/omarchy/current/theme"));
        write(state+"/omarchy/current/theme/colors.toml","accent = \"#ee7a38\"\nbackground = \"#fafafa\"\nmode = \"light\"\n");
        write(state+"/omarchy/current/theme.name","ember\n");
        QCOMPARE(Backend::omarchyThemeDirectory(),state+"/omarchy/current/theme");
        const auto before=QFileInfo(state+"/omarchy/current/theme/colors.toml").lastModified();
        Backend backend(QString(),nullptr,false);
        const auto theme=backend.omarchyTheme();
        QVERIFY(theme.value("available").toBool());
        QCOMPARE(theme.value("name").toString(),QString("ember"));QCOMPARE(theme.value("accent").toString(),QString("#ee7a38"));
        QCOMPARE(theme.value("mode").toString(),QString("light"));
        QCOMPARE(QFileInfo(state+"/omarchy/current/theme/colors.toml").lastModified(),before);
        // A theme without an accent or a background is no theme to follow.
        write(state+"/omarchy/current/theme/colors.toml","foreground = \"#000000\"\n");
        backend.reloadOmarchyTheme();
        QVERIFY(!backend.omarchyTheme().value("available").toBool());
        // The choice to follow it is off by default and goes with the exported settings.
        QVERIFY(!backend.followOmarchyAccent());
        backend.setFollowOmarchyAccent(true);QVERIFY(backend.followOmarchyAccent());
        const auto path=root.filePath("settings.json");
        QCOMPARE(backend.exportSettings(path),QString());
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));
        QCOMPARE(QJsonDocument::fromJson(file.readAll()).object().value("settings").toObject().value("appearance/omarchyAccent"),QJsonValue(true));
        backend.setFollowOmarchyAccent(false);
        QCOMPARE(backend.importSettings(path).value("applied").toInt()>=1,true);
        QVERIFY(backend.followOmarchyAccent());
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
