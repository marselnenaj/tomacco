#include "backend.h"
#include <QClipboard>
#include <QDateTime>
#ifdef TOMACCO_HAVE_DBUS
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#endif
#include <QDesktopServices>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFont>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QImage>
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
// The preferences that move to another machine, each with its check. Paths,
// window sizes, recent projects, job history and research settings belong to
// this machine and stay; Tomacco keeps no credentials in its settings.
static const QString settingsFormat=QStringLiteral("tomacco-desktop-settings");
static bool portableSetting(const QString &key,const QJsonValue &value,QVariant *out) {
    static const QStringList switches{QStringLiteral("Notifications/jobs"),QStringLiteral("NativeDisplay/showDeviceFrame"),QStringLiteral("NativeDisplay/showActivity"),
        QStringLiteral("NativeDisplay/showTouches"),QStringLiteral("NativeAudio/forwardAudio")};
    static const QRegularExpression identifier(QStringLiteral("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));
    if(switches.contains(key)) { if(!value.isBool()) return false; *out=value.toBool(); return true; }
    const auto text=value.toString();
    if(!value.isString()) return false;
    if(key==QLatin1String("appearance") && QStringList{"system","dark","light"}.contains(text)) { *out=text; return true; }
    if(key==QLatin1String("language") && languageChoices.contains(text)) { *out=text; return true; }
    if((key==QLatin1String("NativeIphone/runnerBundle") || key==QLatin1String("device")) && identifier.match(text).hasMatch()) { *out=text; return true; }
    if(key==QLatin1String("updates/mode") && QStringList{"off","notify","auto"}.contains(text)) { *out=text; return true; }
    return false;
}
QString Backend::exportSettings(const QString &path,const QVariantMap &extra) {
    QJsonObject values;
    QVariant checked;
    for(const auto &key:{"appearance","language","device","Notifications/jobs","NativeDisplay/showDeviceFrame","NativeDisplay/showActivity","NativeDisplay/showTouches","NativeAudio/forwardAudio","NativeIphone/runnerBundle"}) {
        if(!m_settings.contains(key)) continue;
        auto value=m_settings.value(key);
        // QSettings keeps switches as the text "true" or "false".
        const auto json=value.toString()==QLatin1String("true") || value.toString()==QLatin1String("false") ? QJsonValue(value.toString()==QLatin1String("true")) : QJsonValue::fromVariant(value);
        if(portableSetting(key,json,&checked)) values.insert(key,json);
    }
    for(auto it=extra.cbegin();it!=extra.cend();++it)
        if(portableSetting(it.key(),QJsonValue::fromVariant(it.value()),&checked)) values.insert(it.key(),QJsonValue::fromVariant(it.value()));
    const QJsonObject document{{"format",settingsFormat},{"version",1},{"application",QStringLiteral("Tomacco")},{"settings",values}};
    QFile file(path);
    if(path.isEmpty() || !file.open(QIODevice::WriteOnly|QIODevice::Truncate)) return tr("%1 cannot be written.").arg(path);
    file.write(QJsonDocument(document).toJson(QJsonDocument::Indented));
    return file.error()==QFile::NoError ? QString() : tr("%1 cannot be written.").arg(path);
}
QVariantMap Backend::importSettings(const QString &path) {
    const auto fail=[](const QString &reason){return QVariantMap{{"ok",false},{"error",reason}};};
    QFile file(path);
    if(path.isEmpty() || !file.open(QIODevice::ReadOnly)) return fail(tr("%1 cannot be read.").arg(path));
    if(file.size()>256*1024) return fail(tr("%1 is too large for a settings file.").arg(path));
    QJsonParseError error;
    const auto document=QJsonDocument::fromJson(file.readAll(),&error).object();
    if(error.error!=QJsonParseError::NoError || document.value("format").toString()!=settingsFormat || !document.value("settings").isObject())
        return fail(tr("%1 is not a Tomacco settings file.").arg(path));
    if(document.value("version").toInt()!=1) return fail(tr("%1 comes from a newer Tomacco; update this one first.").arg(path));
    int applied=0,ignored=0;
    QVariantMap extra;
    const auto values=document.value("settings").toObject();
    for(auto it=values.begin();it!=values.end();++it) {
        QVariant value;
        if(!portableSetting(it.key(),it.value(),&value)) { ++ignored; continue; }
        ++applied;
        if(it.key()==QLatin1String("updates/mode")) extra.insert(it.key(),value);
        else if(it.key()==QLatin1String("language")) setLanguage(value.toString());
        else if(it.key()==QLatin1String("appearance")) setAppearance(value.toString());
        else m_settings.setValue(it.key(),value);
    }
    m_settings.sync();
    return {{"ok",true},{"error",QString()},{"applied",applied},{"ignored",ignored},{"extra",extra}};
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
bool Backend::copyImage(const QString &path) {
    const QImage image(path);
    if(image.isNull()) return false;
    QGuiApplication::clipboard()->setImage(image);
    return true;
}
QString Backend::screenshotPath(const QString &model) {
    auto directory=QStandardPaths::writableLocation(QStandardPaths::PicturesLocation)+"/Tomacco";
    if(!QDir().mkpath(directory)) { emit message(tr("The screenshot folder could not be created.")); return {}; }
    auto safe=model; safe.replace(QRegularExpression("[^A-Za-z0-9_-]"),"-");
    return directory+"/"+safe+"-"+QDateTime::currentDateTime().toString("yyyyMMdd-HHmmss-zzz")+".png";
}
QStringList Backend::recentProjects() const {
    QStringList existing;
    for(const auto &path: m_settings.value("recentProjects").toStringList())
        if(QFileInfo(path).isDir()) existing.append(path);
    return existing;
}
void Backend::rememberProject(const QString &path) {
    const QFileInfo folder(path);
    if(path.isEmpty() || !folder.isDir()) return;
    const auto canonical=folder.canonicalFilePath();
    auto stored=m_settings.value("recentProjects").toStringList();
    if(!stored.isEmpty() && stored.first()==canonical) return;
    stored.removeAll(canonical);
    stored.prepend(canonical);
    m_settings.setValue("recentProjects",stored.mid(0,8));
    emit recentProjectsChanged();
}
void Backend::forgetProject(const QString &path) {
    auto stored=m_settings.value("recentProjects").toStringList();
    if(!stored.removeAll(path)) return;
    m_settings.setValue("recentProjects",stored);
    emit recentProjectsChanged();
}
QString Backend::projectFolder(const QString &path) const {
    const QFileInfo folder(path);
    if(path.isEmpty() || !folder.isDir() || folder.suffix().compare(QLatin1String("app"),Qt::CaseInsensitive)==0) return {};
    return folder.canonicalFilePath();
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
QString Backend::jobLogPath(const QString &path) const {
    static const QRegularExpression name(QStringLiteral("^[0-9a-f]{32}\\.log$"));
    const QFileInfo file(path);
    if(path.isEmpty() || !file.isFile() || file.isSymLink() || !name.match(file.fileName()).hasMatch() || file.dir().dirName()!=QLatin1String("jobs")) return {};
    return file.canonicalFilePath();
}
bool Backend::openJobLog(const QString &path) {
    const auto log=jobLogPath(path);
    if(log.isEmpty()) { emit message(tr("This job has no log file yet.")); return false; }
    return QDesktopServices::openUrl(QUrl::fromLocalFile(log));
}
bool Backend::notify(const QString &title,const QString &body,const QString &tag) {
    emit notificationSent(title,body,tag);
    if(qGuiApp && QGuiApplication::platformName()==QLatin1String("offscreen")) return false;
#ifdef TOMACCO_HAVE_DBUS
    auto bus=QDBusConnection::sessionBus();
    if(!bus.isConnected()) return false;
    const auto service=QStringLiteral("org.freedesktop.Notifications"),path=QStringLiteral("/org/freedesktop/Notifications");
    if(!m_notificationsConnected)
        m_notificationsConnected=bus.connect(service,path,service,QStringLiteral("ActionInvoked"),this,SLOT(notificationAction(uint,QString)));
    auto call=QDBusMessage::createMethodCall(service,path,service,QStringLiteral("Notify"));
    const QVariantMap hints{{QStringLiteral("desktop-entry"),QStringLiteral("io.github.marselnenaj.tomacco")}};
    call<<QCoreApplication::applicationName()<<uint(0)<<QStringLiteral("io.github.marselnenaj.tomacco")<<title<<body
        <<QStringList{QStringLiteral("default"),tr("Open")}<<hints<<int(-1);
    auto *watcher=new QDBusPendingCallWatcher(bus.asyncCall(call),this);
    connect(watcher,&QDBusPendingCallWatcher::finished,this,[this,tag](QDBusPendingCallWatcher *done){
        QDBusPendingReply<uint> reply=*done;
        if(reply.isValid()) {
            m_notifications.insert(reply.value(),tag);
            // Only the recent ones can still be clicked.
            while(m_notifications.size()>32) m_notifications.erase(m_notifications.begin());
        }
        done->deleteLater();
    });
    return true;
#else
    Q_UNUSED(title) Q_UNUSED(body) Q_UNUSED(tag)
    return false;
#endif
}
void Backend::notificationAction(uint id,const QString &action) {
    const auto tag=m_notifications.take(id);
    if(!tag.isEmpty() && action==QLatin1String("default")) emit notificationActivated(tag);
}
void Backend::openExternal(const QUrl &url) { if(url.scheme()=="http" || url.scheme()=="https") QDesktopServices::openUrl(url); }
void Backend::openScreenshots() { QDesktopServices::openUrl(QUrl::fromLocalFile(QStandardPaths::writableLocation(QStandardPaths::PicturesLocation)+"/Tomacco")); }
void Backend::testResult(const QVariantMap &result) { const auto data=QJsonDocument(QJsonObject::fromVariantMap(result)).toJson(QJsonDocument::Compact); fprintf(stdout,"TOMACCO_TEST %s\n",data.constData()); fflush(stdout); }
