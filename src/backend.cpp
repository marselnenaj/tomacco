#include "backend.h"
#include <QClipboard>
#include <QDateTime>
#include <QDesktopServices>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFont>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLibraryInfo>
#include <QLocale>
#include <QMimeDatabase>
#include <QProcess>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QStyleHints>
#include <QTcpSocket>
#include <QTimer>
#include <QTranslator>
#include <cstdio>
#include <memory>

Backend::Backend(QString initialUrl, QObject *parent, bool autoProbe): QObject(parent), m_initial(std::move(initialUrl)) {
    reloadModels();
    connect(&m_server,&QTcpServer::newConnection,this,&Backend::acceptConnections);
    if(qGuiApp) connect(QGuiApplication::styleHints(),&QStyleHints::colorSchemeChanged,this,&Backend::appearanceChanged);
    if(autoProbe) QTimer::singleShot(0,this,&Backend::probeToolchain);
}
void Backend::reloadModels() {
    QFile file(":/assets/models.json");
    if(file.open(QIODevice::ReadOnly)) m_models=QJsonDocument::fromJson(file.readAll()).array().toVariantList();
    QFile custom(dataDirectory()+"/profiles.json");
    if(custom.size()<2*1024*1024 && custom.open(QIODevice::ReadOnly)) {
        const auto values=QJsonDocument::fromJson(custom.readAll()).array();
        for(const auto &value:values) {
            const auto model=value.toObject();
            if(model.value("width").toInt()>=160 && model.value("height").toInt()>=160 &&
               !model.value("colors").toArray().isEmpty() && model.value("colors").toArray().size()==model.value("colorNames").toArray().size())
                m_models.append(model.toVariantMap());
        }
    }
    emit modelsChanged();
}
QString Backend::s_uiLanguage=QStringLiteral("en");
static const QStringList languageChoices{QStringLiteral("system"),QStringLiteral("en"),QStringLiteral("de")};
static QString resolveLanguage(const QString &choice) {
    if(choice==QLatin1String("en") || choice==QLatin1String("de")) return choice;
    // The first desktop language the interface has, English otherwise.
    for(const auto &name: QLocale::system().uiLanguages()) {
        const auto language=QLocale(name).language();
        if(language==QLocale::German) return QStringLiteral("de");
        if(language==QLocale::English) return QStringLiteral("en");
    }
    return QStringLiteral("en");
}
QString Backend::storedLanguage() {
    const auto value=QSettings().value("language","system").toString();
    return languageChoices.contains(value)?value:QStringLiteral("system");
}
QString Backend::dataDirectory() {
    if(qEnvironmentVariableIsSet("TOMACCO_HOME")) return qEnvironmentVariable("TOMACCO_HOME");
    return QDir::homePath()+"/.local/share/tomacco";
}
QString Backend::applyLanguage(const QString &choice) {
    // English is the source language; its file only carries plural forms.
    static QList<QTranslator *> installed;
    for(auto *translator: std::as_const(installed)) { QCoreApplication::removeTranslator(translator); delete translator; }
    installed.clear();
    const auto forced=qEnvironmentVariable("TOMACCO_LANGUAGE");
    const auto language=resolveLanguage(languageChoices.contains(forced)?forced:choice);
    auto *own=new QTranslator;
    if(own->load(":/i18n/tomacco_"+language+".qm")) installed.append(own);
    else { delete own; fprintf(stderr,"Tomacco: no %s translation in this build\n",qPrintable(language)); }
    if(language!=QLatin1String("en")) {
        // Qt's own texts (standard dialog buttons), where the system has them.
        auto *qt=new QTranslator;
        if(qt->load("qtbase_"+language,QLibraryInfo::path(QLibraryInfo::TranslationsPath))) installed.append(qt);
        else delete qt;
    }
    for(auto *translator: std::as_const(installed)) QCoreApplication::installTranslator(translator);
    s_uiLanguage=language;
    return language;
}
QString Backend::language() const { return storedLanguage(); }
void Backend::setLanguage(const QString &choice) {
    const auto value=languageChoices.contains(choice)?choice:QStringLiteral("system");
    m_settings.setValue("language",value);
    applyLanguage(value);
    emit languageChanged();
    emit toolchainChanged();
}
QString Backend::appearance() const {
    const auto value=m_settings.value("appearance","system").toString();
    return value==QLatin1String("dark") || value==QLatin1String("light")?value:QStringLiteral("system");
}
bool Backend::dark() const {
    const auto forced=qEnvironmentVariable("TOMACCO_THEME");
    const auto choice=forced==QLatin1String("dark") || forced==QLatin1String("light")?forced:appearance();
    if(choice!=QLatin1String("system")) return choice==QLatin1String("dark");
    return !qGuiApp || QGuiApplication::styleHints()->colorScheme()!=Qt::ColorScheme::Light;
}
void Backend::setAppearance(const QString &choice) {
    m_settings.setValue("appearance",choice==QLatin1String("dark") || choice==QLatin1String("light")?choice:QStringLiteral("system"));
    emit appearanceChanged();
}
void Backend::applyInterfaceFont() {
    // Bundled, so the interface looks the same everywhere (THIRD_PARTY_NOTICES.md).
    QFontDatabase::addApplicationFont(QStringLiteral(":/assets/fonts/archivo-var.ttf"));
    QFontDatabase::addApplicationFont(QStringLiteral(":/assets/fonts/jetbrains-mono-var.ttf"));
    QFont font(QStringList{QStringLiteral("Archivo"),QStringLiteral("Adwaita Sans"),QStringLiteral("Noto Sans"),QStringLiteral("DejaVu Sans")});
    font.setPixelSize(13);
    QGuiApplication::setFont(font);
}
QString Backend::probeLabel(const QString &value) {
    if(value==QLatin1String("@probe:checking")) return tr("Checking…");
    if(value==QLatin1String("@probe:missing")) return tr("Not installed");
    if(value==QLatin1String("@probe:failed")) return tr("Check failed");
    if(value==QLatin1String("@probe:unknown")) return tr("Unknown");
    return value;
}
QVariant Backend::setting(const QString &key,const QVariant &fallback) { return m_settings.value(key,fallback); }
void Backend::saveSetting(const QString &key,const QVariant &value) { m_settings.setValue(key,value); }
bool Backend::allowedUrl(const QUrl &url) const {
    return url.scheme()=="http" || url.scheme()=="https" || url==demoUrl() || url.toString()=="about:blank";
}
QUrl Backend::normalizeUrl(const QString &input) {
    QString text=input.trimmed();
    if(text.isEmpty() || text=="demo" || text=="tomacco://demo") return demoUrl();
    if(!text.contains("://")) text=(text.startsWith("localhost") || text.startsWith("127.") || text.startsWith("[::1]")) ? "http://"+text : "https://"+text;
    QUrl url(text);
    if(!url.isValid() || url.host().isEmpty() || !allowedUrl(url)) {
        emit message(tr("Enter a valid http:// or https:// address.")); return {};
    }
    return url;
}
QUrl Backend::serveFolder(const QUrl &folder) {
    QDir directory(folder.toLocalFile());
    QString root;
    for(const auto &suffix: {QString(),QString("/build/web"),QString("/dist"),QString("/public")}) {
        QFileInfo index(directory.absolutePath()+suffix+"/index.html");
        if(index.isFile()) { root=index.dir().canonicalPath(); break; }
    }
    if(root.isEmpty()) { emit message(tr("No index.html found. Choose a built web folder or open the dev server address.")); return {}; }
    if(!m_server.isListening() && !m_server.listen(QHostAddress::LocalHost,0)) { emit message(tr("The local preview server could not start.")); return {}; }
    m_root=root;
    return QUrl(QString("http://127.0.0.1:%1/").arg(m_server.serverPort()));
}
void Backend::acceptConnections() {
    while(m_server.hasPendingConnections()) {
        auto *socket=m_server.nextPendingConnection();
        connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
        QTimer::singleShot(10000,socket,[socket]{socket->disconnectFromHost();});
        auto buffer=std::make_shared<QByteArray>();
        connect(socket,&QTcpSocket::readyRead,socket,[this,socket,buffer] {
            *buffer+=socket->readAll();
            if(buffer->size()>32768) { socket->disconnectFromHost(); return; }
            if(!buffer->contains("\r\n\r\n")) return;
            QObject::disconnect(socket,&QTcpSocket::readyRead,nullptr,nullptr);
            auto fields=buffer->split('\n').first().trimmed().split(' ');
            auto respond=[socket](int code,const QByteArray &reason,const QByteArray &type,const QByteArray &body,bool head=false) {
                QByteArray headers="HTTP/1.1 "+QByteArray::number(code)+" "+reason+"\r\nContent-Type: "+type+"\r\nContent-Length: "+QByteArray::number(body.size())+"\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n";
                socket->write(headers); if(!head) socket->write(body); socket->disconnectFromHost();
            };
            if(fields.size()<3 || (fields[0]!="GET" && fields[0]!="HEAD")) { respond(405,"Method Not Allowed","text/plain","GET and HEAD only"); return; }
            // Browser-origin checks complement loopback binding; the requested host
            // must name this server, never an arbitrary DNS-rebinding domain.
            QByteArray host;
            for(const auto &line: buffer->split('\n')) if(line.toLower().startsWith("host:")) host=line.mid(5).trimmed();
            const auto expected=QByteArray("127.0.0.1:")+QByteArray::number(m_server.serverPort());
            if(host!=expected) { respond(403,"Forbidden","text/plain","Invalid host"); return; }
            QString path=QUrl::fromEncoded(fields[1]).path(QUrl::FullyDecoded);
            if(path.contains(QChar::Null) || path.split('/').contains("..") || path.contains('\\')) { respond(403,"Forbidden","text/plain","Invalid path"); return; }
            if(path.endsWith('/')) path+="index.html";
            QFileInfo info(m_root+path);
            if(!info.exists() && QFileInfo(path).suffix().isEmpty()) info.setFile(m_root+"/index.html");
            const QString canonical=info.canonicalFilePath();
            if(canonical.isEmpty() || !canonical.startsWith(m_root+"/") || !info.isFile()) { respond(404,"Not Found","text/plain","Not found"); return; }
            // Source control, credentials and hidden files are never preview assets.
            // Both the requested and the resolved name: a visible symlink in the
            // folder (app.txt -> .env) must not serve the hidden file it names.
            for(const QString &part: path.split('/')+canonical.mid(m_root.size()).split('/'))
                if(part.startsWith('.')) { respond(403,"Forbidden","text/plain","Hidden file"); return; }
            QFile file(canonical);
            if(info.size()>128*1024*1024 || !file.open(QIODevice::ReadOnly)) { respond(413,"Content Too Large","text/plain","Asset unavailable"); return; }
            auto mime=QMimeDatabase().mimeTypeForFile(info).name().toUtf8();
            respond(200,"OK",mime,file.readAll(),fields[0]=="HEAD");
        });
    }
}
void Backend::probe(const QString &name,const QStringList &args,QString *destination) {
    QString path=QDir::homePath()+"/.local/bin/"+name;
    if(!QFileInfo::exists(path)) path=QStandardPaths::findExecutable(name);
    if(path.isEmpty()) { *destination=QStringLiteral("@probe:missing"); emit toolchainChanged(); return; }
    auto *process=new QProcess(this); ++m_pending;
    auto *timer=new QTimer(process); timer->setSingleShot(true); timer->start(15000);
    auto finished=std::make_shared<bool>(false);
    auto complete=[this,process,destination,finished](bool ok) {
        if(*finished) return;
        *finished=true;
        auto text=QString::fromUtf8(process->readAllStandardOutput()).trimmed();
        *destination=!ok?QStringLiteral("@probe:failed"):text.isEmpty()?QStringLiteral("@probe:unknown"):text.section('\n',0,0);
        --m_pending; emit toolchainChanged(); process->deleteLater();
    };
    connect(process,qOverload<int,QProcess::ExitStatus>(&QProcess::finished),this,[complete](int code,QProcess::ExitStatus status){ complete(code==0 && status==QProcess::NormalExit); });
    connect(process,&QProcess::errorOccurred,this,[complete](QProcess::ProcessError){ complete(false); });
    connect(timer,&QTimer::timeout,process,[process]{ process->kill(); });
    process->start(path,args);
}
void Backend::probeToolchain() {
    if(m_pending) return;
    probe("xtool",{"--version"},&m_xtool);
    probe("swift",{"--version"},&m_swift);
    probe("xtool",{"sdk","status"},&m_sdk);
    emit toolchainChanged();
}
void Backend::copy(const QString &text) { QGuiApplication::clipboard()->setText(text); emit message(tr("Copied")); }
QString Backend::screenshotPath(const QString &model) {
    auto directory=QStandardPaths::writableLocation(QStandardPaths::PicturesLocation)+"/Tomacco";
    if(!QDir().mkpath(directory)) { emit message(tr("The screenshot folder could not be created.")); return {}; }
    auto safe=model; safe.replace(QRegularExpression("[^A-Za-z0-9_-]"),"-");
    return directory+"/"+safe+"-"+QDateTime::currentDateTime().toString("yyyyMMdd-HHmmss-zzz")+".png";
}
QString Backend::qtVersion() const { return QString::fromLatin1(qVersion()); }
QUrl Backend::sourceUrl() const {
    const QString repository=QStringLiteral("https://github.com/marselnenaj/tomacco");
    const auto version=QCoreApplication::applicationVersion();
    return QUrl(version.isEmpty()?repository:repository+QStringLiteral("/releases/tag/v")+version);
}
QString Backend::documentPath(const QString &name) const {
    static const QStringList documents{QStringLiteral("LICENSE"),QStringLiteral("THIRD_PARTY_NOTICES.md"),QStringLiteral("legal.md"),QStringLiteral("RELEASE_NOTES.md")};
    if(!documents.contains(name)) return {};
    const auto application=QCoreApplication::applicationDirPath();
    // CMake's package (bin/), install.py (~/.local/lib/tomacco/) and,
    // for a build tree, the sources the binary was built from.
    QStringList directories{application+"/../share/doc/tomacco",application+"/../../share/doc/tomacco",
        QDir::homePath()+"/.local/share/doc/tomacco"};
    // A build directory inside the source tree (build-product/, build/).
    directories<<application+"/.."<<application+"/../docs";
#ifdef TOMACCO_SOURCE_DIR
    directories<<QStringLiteral(TOMACCO_SOURCE_DIR)<<QStringLiteral(TOMACCO_SOURCE_DIR "/docs");
#endif
    for(const auto &directory:directories) {
        const QFileInfo file(directory+"/"+name);
        if(file.isFile()) return file.canonicalFilePath();
    }
    return {};
}
bool Backend::openDocument(const QString &name) {
    const auto path=documentPath(name);
    if(path.isEmpty()) { emit message(tr("%1 is not part of this installation.").arg(name)); return false; }
    return QDesktopServices::openUrl(QUrl::fromLocalFile(path));
}
bool Backend::relaunch() {
    auto program=QCoreApplication::applicationFilePath();
    // How the kernel names a running program whose file an update replaced.
    if(program.endsWith(QLatin1String(" (deleted)"))) program.chop(10);
    return QProcess::startDetached(program,QCoreApplication::arguments().mid(1));
}
void Backend::openExternal(const QUrl &url) { if(url.scheme()=="http" || url.scheme()=="https") QDesktopServices::openUrl(url); }
void Backend::openScreenshots() { QDesktopServices::openUrl(QUrl::fromLocalFile(QStandardPaths::writableLocation(QStandardPaths::PicturesLocation)+"/Tomacco")); }
void Backend::testResult(const QVariantMap &result) { const auto data=QJsonDocument(QJsonObject::fromVariantMap(result)).toJson(QJsonDocument::Compact); fprintf(stdout,"TOMACCO_TEST %s\n",data.constData()); fflush(stdout); }
