#pragma once
#include <QHash>
#include <QObject>
#include <QSettings>
#include <QTcpServer>
#include <QUrl>
#include <QVariantList>
#include <QProcessEnvironment>
#include <functional>

class Backend final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList models READ models NOTIFY modelsChanged)
    Q_PROPERTY(QString xtool READ xtool NOTIFY toolchainChanged)
    Q_PROPERTY(QString swift READ swift NOTIFY toolchainChanged)
    Q_PROPERTY(QString sdk READ sdk NOTIFY toolchainChanged)
    Q_PROPERTY(bool probing READ probing NOTIFY toolchainChanged)
    Q_PROPERTY(QUrl demoUrl READ demoUrl CONSTANT)
    Q_PROPERTY(QString initialUrl READ initialUrl CONSTANT)
    // The stored interface language choice: "system" or a language code ("en", "de", ...).
    Q_PROPERTY(QString language READ language NOTIFY languageChanged)
    // The language the interface shows, as a code: "en", "de", ...
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
    // True when this desktop found no stored settings at all: Tomacco's very first start here.
    Q_PROPERTY(bool firstStart READ firstStart CONSTANT)
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
    // Installs the translators for a choice ("system", "en", "de", ...; anything
    // else counts as "system") and returns the effective language. The
    // TOMACCO_LANGUAGE environment variable overrides the choice.
    static QString applyLanguage(const QString &choice);
    // The stored choice, without a Backend (main() needs it before parsing
    // the command line).
    static QString storedLanguage();
    // The data directory the control plane uses: TOMACCO_HOME, then the default.
    static QString dataDirectory();
    void setInitialUrl(const QString &value) { m_initial=value; }
    // Writes the portable preferences (theme, language, notifications, device
    // view choices; no paths, window sizes, history or credentials) and the
    // extra values given (updates/mode) to a JSON file. An empty string on
    // success, otherwise the reason.
    Q_INVOKABLE QString exportSettings(const QString &path,const QVariantMap &extra={});
    // Reads such a file and applies the values it may carry, each checked;
    // anything else is ignored. Returns {ok, error, applied, ignored, extra}.
    Q_INVOKABLE QVariantMap importSettings(const QString &path);
    Q_INVOKABLE QVariant setting(const QString &key, const QVariant &fallback={});
    Q_INVOKABLE void saveSetting(const QString &key, const QVariant &value);
    Q_INVOKABLE QUrl normalizeUrl(const QString &input);
    Q_INVOKABLE bool allowedUrl(const QUrl &url) const;
    Q_INVOKABLE QUrl serveFolder(const QUrl &folder);
    Q_INVOKABLE void probeToolchain();
    Q_INVOKABLE void copy(const QString &text);
    // The text on the clipboard, empty when it holds none.
    Q_INVOKABLE QString clipboardText() const;
    Q_INVOKABLE QString screenshotPath(const QString &model);
    // Puts the image saved at this path on the clipboard; false when it cannot be read.
    Q_INVOKABLE bool copyImage(const QString &path);
    Q_INVOKABLE void openExternal(const QUrl &url);
    // Shows a desktop notification (org.freedesktop.Notifications) with an
    // Open action; tag comes back with notificationActivated when it is
    // clicked. False when no notification service is reachable. An offscreen
    // run has no desktop to notify: it only emits notificationSent.
    Q_INVOKABLE bool notify(const QString &title,const QString &body,const QString &tag);
    // Starts Tomacco again for the new version an update installed: through the
    // installed launcher when this is an installed copy, otherwise this program,
    // with its arguments and the given project and workspace. The new process
    // waits until this one has ended (TOMACCO_RESTART_AFTER), so it gets the
    // agent session.
    Q_INVOKABLE bool relaunch(const QString &project={},const QString &workspace={});
    // What relaunch starts, for a program at this path in this home directory.
    static QString relaunchProgram(const QString &applicationPath,const QString &home);
    // The arguments it passes: the original ones without a project, workspace,
    // capture or smoke test, then the project and workspace given.
    static QStringList relaunchArguments(const QStringList &original,const QString &project,const QString &workspace);
    // Replaces how relaunch starts the program (the tests record it instead).
    using Starter=std::function<bool(const QString &program,const QStringList &arguments,const QProcessEnvironment &environment)>;
    void setStarter(Starter starter) { m_starter=std::move(starter); }
    // The Highlights of the installed release notes when they are this
    // version's, as Markdown; otherwise an empty string.
    Q_INVOKABLE QString releaseHighlights(const QString &version) const;
    bool firstStart() const { return m_firstStart; }
    Q_INVOKABLE void openScreenshots();
    // Where a screen recording of this device goes: Videos/Tomacco/<model>-<time>.mp4.
    Q_INVOKABLE QString recordingPath(const QString &model);
    // A file for the latest picture of a device in the device grid, in the
    // cache directory; the same file each time for the same device.
    Q_INVOKABLE QString thumbnailPath(const QString &device);
    // Shows a file selected in the file manager (org.freedesktop.FileManager1),
    // or opens its folder; false when the file is missing. An offscreen run
    // only emits fileRevealed.
    Q_INVOKABLE bool revealFile(const QString &path);
    // The entries an agent's session log (TOMACCO_SESSION_LOG) gained from
    // offset on: {entries, next_offset, reset, error}. Only whole lines are
    // read, at most 256 KiB at a time; a line that is no JSON object is
    // skipped. A file shorter than offset was started anew: it is read from
    // the beginning and reset is true.
    Q_INVOKABLE QVariantMap readSessionLog(const QString &path, qint64 offset) const;
    // The session log this desktop was started with, if any.
    Q_INVOKABLE QString sessionLogFromEnvironment() const;
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
    void fileRevealed(const QString &path);
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
    bool m_firstStart=false;
    Starter m_starter;
    QVariantList m_models;
    QString m_xtool=QStringLiteral("@probe:checking"), m_swift=m_xtool, m_sdk=m_xtool;
    QString m_initial, m_root;
    QTcpServer m_server;
    int m_pending=0;
};
