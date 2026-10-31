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
