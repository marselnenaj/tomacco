#pragma once
#include <QHash>
#include <QObject>
#include <QSettings>
#include <QTcpServer>
#include <QUrl>
#include <QVariantList>

class Backend final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList models READ models NOTIFY modelsChanged)
    Q_PROPERTY(QString xtool READ xtool NOTIFY toolchainChanged)
    Q_PROPERTY(QString swift READ swift NOTIFY toolchainChanged)
    Q_PROPERTY(QString sdk READ sdk NOTIFY toolchainChanged)
    Q_PROPERTY(bool probing READ probing NOTIFY toolchainChanged)
    Q_PROPERTY(QUrl demoUrl READ demoUrl CONSTANT)
    Q_PROPERTY(QString initialUrl READ initialUrl CONSTANT)
    // The stored interface language choice: "system", "en" or "de".
    Q_PROPERTY(QString language READ language NOTIFY languageChanged)
    // The language the interface shows: "en" or "de".
    Q_PROPERTY(QString uiLanguage READ uiLanguage NOTIFY languageChanged)
    // The stored appearance choice: "system", "dark" or "light".
    Q_PROPERTY(QString appearance READ appearance NOTIFY appearanceChanged)
    // Whether the interface is dark now: the choice, or the desktop's scheme
    // ("system"; dark when the desktop states none). TOMACCO_THEME=dark|light
    // overrides it for one run.
    Q_PROPERTY(bool dark READ dark NOTIFY appearanceChanged)
    // For the About dialog.
    Q_PROPERTY(QString qtVersion READ qtVersion CONSTANT)
    // Project folders opened before, newest first; folders that are gone are left out.
    Q_PROPERTY(QStringList recentProjects READ recentProjects NOTIFY recentProjectsChanged)
    Q_PROPERTY(QUrl sourceUrl READ sourceUrl CONSTANT)
public:
    explicit Backend(QString initialUrl, QObject *parent=nullptr, bool autoProbe=true);
    QVariantList models() const { return m_models; }
    QString xtool() const { return probeLabel(m_xtool); }
    QString swift() const { return probeLabel(m_swift); }
    QString sdk() const { return probeLabel(m_sdk); }
    bool probing() const { return m_pending > 0; }
    QUrl demoUrl() const { return QUrl("qrc:/assets/demo.html"); }
    QString initialUrl() const { return m_initial; }
    QString language() const;
    QString uiLanguage() const { return s_uiLanguage; }
    QString appearance() const;
    bool dark() const;
    Q_INVOKABLE void setAppearance(const QString &choice);
    // The interface font for everything that inherits no font from a control
    // (qml/Theme.js lists the same families); loads the bundled fonts. Call it before QML is loaded.
    static void applyInterfaceFont();
    QString qtVersion() const;
    QStringList recentProjects() const;
    // Puts a project folder at the front of the recent ones (at most eight are kept).
    Q_INVOKABLE void rememberProject(const QString &path);
    Q_INVOKABLE void forgetProject(const QString &path);
    // The folder a dropped path names, to open as a project: its canonical
    // path when it is a directory and no app bundle, otherwise an empty string.
    Q_INVOKABLE QString projectFolder(const QString &path) const;
    // The release this build's version names, where its corresponding source
    // is offered (docs/SOURCE.md.in); the repository when no version is set.
    QUrl sourceUrl() const;
    // The installed copy of a document the About dialog opens (LICENSE,
    // THIRD_PARTY_NOTICES.md, legal.md or RELEASE_NOTES.md), or an empty string when this installation has none.
    Q_INVOKABLE QString documentPath(const QString &name) const;
    // Opens that document in the desktop's viewer; false when it is missing.
    Q_INVOKABLE bool openDocument(const QString &name);
    // A job's log file as job.list names it (DATA/jobs/<id>.log), or an empty
    // string for anything else; openJobLog opens it in the desktop's viewer.
    Q_INVOKABLE QString jobLogPath(const QString &path) const;
    Q_INVOKABLE bool openJobLog(const QString &path);
    // Persists the choice and installs its translators. The application's
    // QML engine retranslates on languageChanged (see main.cpp).
    Q_INVOKABLE void setLanguage(const QString &choice);
    // Installs the translators for a choice ("system", "en", "de"; anything
    // else counts as "system") and returns the effective language. The
    // TOMACCO_LANGUAGE environment variable overrides the choice.
    static QString applyLanguage(const QString &choice);
    // The stored choice, without a Backend (main() needs it before parsing
    // the command line).
    static QString storedLanguage();
    // The data directory the control plane uses: TOMACCO_HOME, then the default.
    static QString dataDirectory();
    void setInitialUrl(const QString &value) { m_initial=value; }
    Q_INVOKABLE QVariant setting(const QString &key, const QVariant &fallback={});
    Q_INVOKABLE void saveSetting(const QString &key, const QVariant &value);
    Q_INVOKABLE QUrl normalizeUrl(const QString &input);
    Q_INVOKABLE bool allowedUrl(const QUrl &url) const;
    Q_INVOKABLE QUrl serveFolder(const QUrl &folder);
    Q_INVOKABLE void probeToolchain();
    Q_INVOKABLE void copy(const QString &text);
    Q_INVOKABLE QString screenshotPath(const QString &model);
    // Puts the image saved at this path on the clipboard; false when it cannot be read.
    Q_INVOKABLE bool copyImage(const QString &path);
    Q_INVOKABLE void openExternal(const QUrl &url);
    // Shows a desktop notification (org.freedesktop.Notifications) with an
    // Open action; tag comes back with notificationActivated when it is
    // clicked. False when no notification service is reachable. An offscreen
    // run has no desktop to notify: it only emits notificationSent.
    Q_INVOKABLE bool notify(const QString &title,const QString &body,const QString &tag);
    // Starts this program again from its path, for the new version an update installed there.
    Q_INVOKABLE bool relaunch();
    Q_INVOKABLE void openScreenshots();
    Q_INVOKABLE void testResult(const QVariantMap &result);
    Q_INVOKABLE void reloadModels();
signals:
    void modelsChanged();
    void toolchainChanged();
    void message(const QString &text);
    void languageChanged();
    void appearanceChanged();
    void recentProjectsChanged();
    void notificationSent(const QString &title,const QString &body,const QString &tag);
    void notificationActivated(const QString &tag);
private slots:
    // The notification service's ActionInvoked signal.
    void notificationAction(uint id,const QString &action);
private:
    static QString s_uiLanguage;
    // A probe result is the tool's output or one of these states, which are
    // translated when read so a language change also reaches them.
    static QString probeLabel(const QString &value);
    void acceptConnections();
    QHash<uint,QString> m_notifications;
    bool m_notificationsConnected=false;
    void probe(const QString &name, const QStringList &args, QString *destination);
    QSettings m_settings;
    QVariantList m_models;
    QString m_xtool=QStringLiteral("@probe:checking"), m_swift=m_xtool, m_sdk=m_xtool;
    QString m_initial, m_root;
    QTcpServer m_server;
    int m_pending=0;
};
