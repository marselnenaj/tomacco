#include "backend.h"
#include "control.h"
#include "nativeview.h"
#include <QCommandLineParser>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>
#include <QTimer>
#include <QElapsedTimer>
#include <QHash>
#include <memory>
#include <QtWebEngineQuick/qtwebenginequickglobal.h>
#include <cstdio>
#include <ctime>
#include <cstring>
#include <QFile>
#include <cerrno>
#include <csignal>
#include <unistd.h>

// The version comes from the build (CMake's project version).
#ifndef TOMACCO_VERSION
#error "TOMACCO_VERSION must be defined by the build"
#endif

// The CPU time this thread has used, in milliseconds: less disturbed by a busy
// host than the time on the clock.
static long mainThreadMs() {
    timespec spent{};
    clock_gettime(CLOCK_THREAD_CPUTIME_ID,&spent);
    return long(spent.tv_sec*1000+spent.tv_nsec/1000000);
}

// Whether a process still runs; a zombie that waits for its parent does not count.
static bool processRuns(qint64 pid) {
    if(kill(pid_t(pid),0)!=0 && errno!=EPERM) return false;
    QFile stat(QStringLiteral("/proc/%1/stat").arg(pid));
    if(!stat.open(QIODevice::ReadOnly)) return true;
    const auto text=stat.readAll();
    const auto close=text.lastIndexOf(')');
    return close<0 || text.mid(close+2,1)!="Z";
}

int main(int argc,char **argv) {
    // TOMACCO_STARTUP_TIMING=1 prints how long the start took on standard
    // error: until the window's first frame, and until it is ready, which is
    // when the control program has also answered the operation list and the
    // first job list. Counted from the start of main().
    QElapsedTimer startup; startup.start();
    // A restart for an update (Backend::relaunch): the previous desktop ends
    // first, so this one gets its agent session. At most 10 seconds.
    if(const auto previous=qEnvironmentVariableIntValue("TOMACCO_RESTART_AFTER");previous>1) {
        qunsetenv("TOMACCO_RESTART_AFTER");
        for(int i=0;i<100 && processRuns(previous);++i) usleep(100000);
    }
    // Without a display Qt aborts with a core dump. --version and --help need
    // none; anything else gets a sentence instead.
    if(qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM") && qEnvironmentVariableIsEmpty("DISPLAY") && qEnvironmentVariableIsEmpty("WAYLAND_DISPLAY")) {
        bool answersOnly=false;
        for(int i=1;i<argc;++i) answersOnly=answersOnly || !strcmp(argv[i],"--version") || !strcmp(argv[i],"-v") || !strcmp(argv[i],"--help") || !strcmp(argv[i],"-h");
        if(!answersOnly) {
            fputs("Tomacco needs a graphical session, and neither DISPLAY nor WAYLAND_DISPLAY is set. tomaccoctl works without one.\n",stderr);
            return 1;
        }
        qputenv("QT_QPA_PLATFORM","offscreen");
        qputenv("QT_QPA_PLATFORMTHEME","generic"); // A GTK theme plugin would exit for the same reason.
    }
    QtWebEngineQuick::initialize();
    QGuiApplication app(argc,argv);
    app.setApplicationName("Tomacco");
    app.setOrganizationName("marselnenaj");
    app.setApplicationVersion(QStringLiteral(TOMACCO_VERSION));
    app.setDesktopFileName("io.github.marselnenaj.tomacco");
    app.setWindowIcon(QIcon(":/assets/icon.svg"));
    Backend::applyInterfaceFont();
    // English unless the desktop (or the stored choice) asks for another language;
    // installed before anything below produces a text.
    Backend::applyLanguage(Backend::storedLanguage());
    QCommandLineParser parser;
    parser.setApplicationDescription(QCoreApplication::translate("main","Tomacco: build, run and test native Android and iOS apps on Linux.\nLicensed under the GNU General Public License, version 2 or later."));
    parser.addHelpOption(); parser.addVersionOption();
    parser.addOption({"url",QCoreApplication::translate("main","Open a web address at startup"),"url"});
    parser.addOption({"folder",QCoreApplication::translate("main","Open a local web build"),"directory"});
    parser.addOption({"device",QCoreApplication::translate("main","Device profile ID from tomaccoctl call catalog.list"),"id"});
    parser.addOption({"preview-session",QCoreApplication::translate("main","Name of the agent preview session"),"name","main"});
    parser.addOption({"project",QCoreApplication::translate("main","Open a project in the native workspace"),"directory"});
    parser.addOption({"workspace",QCoreApplication::translate("main","Open a workspace: runtime, project, devices, ios-research, jobs, tools, debug, testing, profiling, files, release or preview"),"tab","preview"});
    parser.addOption({"orientation",QCoreApplication::translate("main","Orientation: portrait or landscape"),"orientation","portrait"});
    parser.addOption({"fold",QCoreApplication::translate("main","Duo display: closed or open"),"state","closed"});
    parser.addOption({"device-only",QCoreApplication::translate("main","Show only the detached device window; the main window stays hidden")});
    parser.addOption({"capture",QCoreApplication::translate("main","Save the window as a PNG for visual review and quit"),"path"});
    parser.addOption({"smoke-test",QCoreApplication::translate("main","Run the preview's interactive checks")});
    parser.process(app);
    if(!QStringList{"portrait","landscape"}.contains(parser.value("orientation")) || !QStringList{"closed","open"}.contains(parser.value("fold"))) {
        fputs(qPrintable(QCoreApplication::translate("main","Invalid orientation or fold position.\n")),stderr); return 2;
    }
    if(!QStringList{"runtime","preview","project","devices","ios-research","jobs","tools","debug","testing","profiling","files","release"}.contains(parser.value("workspace"))) {
        fputs(qPrintable(QCoreApplication::translate("main","Unknown workspace. See --help for the available workspaces.\n")),stderr); return 2;
    }
    Backend backend(parser.value("url"));
    if(parser.isSet("device")) {
        bool found=false;
        for(const auto &value:backend.models()) if(value.toMap().value("id").toString()==parser.value("device"))found=true;
        if(!found) { fputs(qPrintable(QCoreApplication::translate("main","Unknown device profile.\n")),stderr); return 2; }
    }
    Control control(parser.value("preview-session"));
    if(parser.isSet("folder")) {
        auto url=backend.serveFolder(QUrl::fromLocalFile(parser.value("folder")));
        if(url.isEmpty()) { fputs(qPrintable(QCoreApplication::translate("main","No web build with index.html found.\n")),stderr); return 2; }
        backend.setInitialUrl(url.toString());
    }
    QQmlApplicationEngine engine;
    engine.setUiLanguage(backend.uiLanguage());
    // A language chosen in the settings applies at once, without a restart.
    QObject::connect(&backend,&Backend::languageChanged,&engine,[&]{engine.setUiLanguage(backend.uiLanguage());engine.retranslate();});
    qmlRegisterType<NativeView>("Tomacco.Native",1,0,"NativeView");
    engine.rootContext()->setContextProperty("Studio",&backend);
    engine.rootContext()->setContextProperty("Agent",&control);
    engine.rootContext()->setContextProperty("initialProject",parser.value("project"));
    const bool previewRequested = parser.isSet("url") || parser.isSet("folder") || parser.isSet("device") || parser.isSet("smoke-test");
    engine.rootContext()->setContextProperty("initialWorkspace",parser.isSet("workspace") || previewRequested ? parser.value("workspace") : QStringLiteral("runtime"));
    engine.rootContext()->setContextProperty("smokeTest",parser.isSet("smoke-test"));
    engine.rootContext()->setContextProperty("captureMode",parser.isSet("capture"));
    engine.rootContext()->setContextProperty("initialDevice",parser.value("device"));
    engine.rootContext()->setContextProperty("initialDeviceOnly",parser.isSet("device-only"));
    engine.rootContext()->setContextProperty("initialLandscape",parser.value("orientation")=="landscape");
    engine.rootContext()->setContextProperty("initialUnfolded",parser.value("fold")=="open");
    QObject::connect(&engine,&QQmlApplicationEngine::objectCreationFailed,&app,[]{QCoreApplication::exit(1);},Qt::QueuedConnection);
    if(qEnvironmentVariableIsSet("TOMACCO_STARTUP_TIMING")) {
        auto marks=std::make_shared<QHash<QString,qint64>>();
        const auto mark=[marks,&startup](const QString &name) {
            if(marks->contains(name)) return;
            marks->insert(name,startup.elapsed());
            fprintf(stderr,"TOMACCO_STARTUP %s %lld ms cpu %ld ms\n",qPrintable(name),static_cast<long long>(marks->value(name)),mainThreadMs());
            if(name!=QLatin1String("ready") && marks->contains("first-frame") && marks->contains("schema") && marks->contains("jobs")) {
                marks->insert("ready",startup.elapsed());
                fprintf(stderr,"TOMACCO_STARTUP ready %lld ms cpu %ld ms\n",static_cast<long long>(marks->value("ready")),mainThreadMs());
                if(qEnvironmentVariableIntValue("TOMACCO_STARTUP_TIMING")==2) QCoreApplication::exit(0);
            }
        };
        QObject::connect(&control,&Control::apiFinished,&app,[mark](int,const QString &operation,const QVariantMap &response) {
            if(operation==QLatin1String("schema")) mark("schema");
            else if(operation==QLatin1String("job.list") && response.value("ok").toBool()) mark("jobs");
        });
        QObject::connect(&engine,&QQmlApplicationEngine::objectCreated,&app,[mark](QObject *object,const QUrl &) {
            mark("qml-loaded");
            auto *window=qobject_cast<QQuickWindow*>(object);
            if(!window) return;
            QObject::connect(window,&QQuickWindow::frameSwapped,window,[mark]{ mark("first-frame"); });
        });
        mark("engine-ready");
    }
    engine.load(QUrl("qrc:/qml/Main.qml"));
    if(parser.isSet("capture")) QTimer::singleShot(4500,&app,[&]{
        for(auto *root:engine.rootObjects()) if(auto *window=qobject_cast<QQuickWindow*>(root)) {
            bool ok=window->grabWindow().save(parser.value("capture")); app.exit(ok?0:2); return;
        }
        app.exit(3);
    });
    return app.exec();
}
