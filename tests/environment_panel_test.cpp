// Local QML contract fixtures only: no Control, CLI or devices. The fake
// agent records every typed call the QA environment panel makes.
#include <QGuiApplication>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QQuickItem>
#include <QJSValue>
#include <QDir>
#include <QtTest>
#include <memory>

class EnvStyle : public QObject {
    Q_OBJECT
    Q_PROPERTY(int uiScale MEMBER scale NOTIFY changed)
    Q_PROPERTY(bool dark MEMBER dark NOTIFY changed)
    Q_PROPERTY(QVariantMap omarchyTheme READ omarchyTheme CONSTANT)
    Q_PROPERTY(bool followOmarchyAccent READ followOmarchyAccent CONSTANT)
public:
    int scale=100;bool dark=true;
    QVariantMap omarchyTheme()const{return {};}
    bool followOmarchyAccent()const{return false;}
signals:void changed();
};
class EnvAgent : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList devices MEMBER devices NOTIFY devicesChanged)
    Q_PROPERTY(QString selectedDevice MEMBER selectedDevice CONSTANT)
public:
    struct Call {int id;QString op;QVariantMap args;};
    QList<Call> calls;int serial=0;QVariantList devices;QString selectedDevice="emulator-5554";
    Q_INVOKABLE int request(QString op,QVariantMap args,bool quiet=true){Q_UNUSED(quiet);calls.append({++serial,op,args});return serial;}
    void finish(Call call,QVariantMap result){emit apiFinished(call.id,call.op,{{"ok",true},{"result",result}});QCoreApplication::processEvents();}
    void fail(Call call,QString message,QVariantMap details={}){emit apiFinished(call.id,call.op,{{"ok",false},{"error",QVariantMap{{"message",message},{"details",details}}}});QCoreApplication::processEvents();}
    QStringList ops()const{QStringList out;for(auto &c:calls)out<<c.op;return out;}
signals:void apiFinished(int id,QString op,QVariantMap response);void devicesChanged();
};

class EnvironmentPanelTest : public QObject {
    Q_OBJECT
    EnvStyle style;EnvAgent agent;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;QQuickItem *page=nullptr;
    QVariant fromJs(QVariant v){return v.metaType()==QMetaType::fromType<QJSValue>()?v.value<QJSValue>().toVariant():v;}
    QObject *object(const char *name){return page->findChild<QObject*>(QString::fromLatin1(name));}
    void invoke(const char *name,QVariant a={}){bool ok=a.isValid()?QMetaObject::invokeMethod(page,name,Q_ARG(QVariant,a)):QMetaObject::invokeMethod(page,name);QVERIFY2(ok,name);QCoreApplication::processEvents();}
    bool enabled(const char *name){return object(name)->property("enabled").toBool();}
    QVariantMap preset(int revision=1){return {{"format","tomacco.environment-preset"},{"version",1},{"id","night-qa"},{"name","Night QA"},{"revision",revision},{"settings",QVariantMap{{"dark_mode","on"},{"talkback",true}}}};}
    QVariantMap entry(QString field,QVariant target,QString action,QVariant value,bool restorable=true){return {{"field",field},{"target",target},{"action",action},{"current",QVariantMap{{"value",value},{"supported",action!="unsupported"},{"restorable",restorable},{"reason",action=="unsupported"?"TalkBack is not installed on this device":""}}}};}
    QVariantMap plan(QStringList unsupported={}){
        QVariantList entries{entry("dark_mode","on","change","off")};entries<<entry("talkback",true,unsupported.contains("talkback")?"unsupported":"change",false);
        QVariantList u;for(auto &f:unsupported)u<<f;
        return {{"plan_id","0123456789abcdef0123456789abcdef"},{"device","emulator-5556"},{"identity",QVariantMap{{"model","sdk_gphone64"}}},{"preset",QVariantMap{{"id","night-qa"},{"name","Night QA"},{"revision",1}}},{"entries",entries},{"unsupported",u},{"not_restorable",QVariantList{}}};
    }
    void loadPreset(){
        QCOMPARE(agent.calls.last().op,QString("android.environment.preset.list"));
        agent.finish(agent.calls.last(),{{"presets",QVariantList{preset()}},{"problems",QVariantList{}}});
        invoke("choose","night-qa");QCOMPARE(page->property("presetId").toString(),QString("night-qa"));
    }
    void target(){
        auto combo=object("environmentDevice");QCOMPARE(combo->property("count").toInt(),2);
        page->setProperty("device","emulator-5556");QCoreApplication::processEvents();QVERIFY(page->property("hasTarget").toBool());
    }
private slots:
    void init(){
        agent.calls.clear();style.scale=100;
        agent.devices={QVariantMap{{"id","emulator-5554"},{"platform","android"},{"state","device"},{"name","Pixel 9"}},
                       QVariantMap{{"id","emulator-5556"},{"platform","android"},{"state","device"},{"name","Pixel Tablet"}},
                       QVariantMap{{"id","R5CT"},{"platform","android"},{"state","unauthorized"},{"name","Phone"}},
                       QVariantMap{{"id","00008110-001"},{"platform","ios"},{"state","connected"},{"name","iPhone"}}};
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);engine->rootContext()->setContextProperty("Agent",&agent);
        QQmlComponent component(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/EnvironmentPanel.qml")));
        QVERIFY2(component.isReady(),qPrintable(component.errorString()));
        page=qobject_cast<QQuickItem*>(component.createWithInitialProperties({{"workspace",QVariant::fromValue(&agent)}}));QVERIFY2(page,qPrintable(component.errorString()));
        window=std::make_unique<QQuickWindow>();window->setColor(QColor("#0a0a09"));window->resize(1100,1400);page->setParentItem(window->contentItem());page->setWidth(window->width());window->show();QTest::qWait(15);
    }
    void cleanup(){window.reset();delete page;page=nullptr;engine.reset();}
    void opening_reads_presets_only_and_chooses_no_device(){
        QCOMPARE(agent.ops(),QStringList{"android.environment.preset.list"});
        // The device selected elsewhere in the app is not a target here.
        QCOMPARE(page->property("device").toString(),QString());
        loadPreset();QVERIFY(!enabled("environmentPlan"));QVERIFY(!enabled("environmentInspect"));QVERIFY(!enabled("environmentApply"));
        QCOMPARE(object("environmentDevice")->property("count").toInt(),2);
        QVERIFY(object("environmentTalkBackNote")->property("text").toString().contains("does not show that your app works with a screen reader"));
    }
    void save_creates_then_updates_with_the_seen_revision(){
        agent.finish(agent.calls.last(),{{"presets",QVariantList{}},{"problems",QVariantList{}}});
        object("environmentId")->setProperty("text","night-qa");object("environmentName")->setProperty("text","Night QA");
        QVERIFY(!enabled("environmentSave"));
        QMetaObject::invokeMethod(page,"setSetting",Q_ARG(QVariant,"dark_mode"),Q_ARG(QVariant,"on"));QCoreApplication::processEvents();
        QVERIFY(enabled("environmentSave"));invoke("save");
        auto call=agent.calls.last();QCOMPARE(call.op,QString("android.environment.preset.save"));QVERIFY(!call.args.contains("if_revision"));
        QCOMPARE(call.args["settings"].toMap()["dark_mode"].toString(),QString("on"));
        agent.finish(call,{{"preset",preset()},{"created",true}});QCOMPARE(page->property("revision").toInt(),1);
        invoke("save");call=agent.calls.last();QCOMPARE(call.op,QString("android.environment.preset.save"));QCOMPARE(call.args["if_revision"].toInt(),1);
    }
    void plan_apply_and_restore_go_to_the_chosen_device_only(){
        loadPreset();target();QVERIFY(enabled("environmentPlan"));
        invoke("makePlan");auto call=agent.calls.last();QCOMPARE(call.op,QString("android.environment.plan"));QCOMPARE(call.args["device"].toString(),QString("emulator-5556"));QCOMPARE(call.args["preset"].toString(),QString("night-qa"));
        QVERIFY(!enabled("environmentApply"));
        agent.finish(call,plan());QVERIFY(page->property("planIsCurrent").toBool());QVERIFY(enabled("environmentApply"));
        invoke("apply");call=agent.calls.last();QCOMPARE(call.op,QString("android.environment.apply"));
        QCOMPARE(call.args["plan_id"].toString(),QString("0123456789abcdef0123456789abcdef"));QCOMPARE(call.args["device"].toString(),QString("emulator-5556"));QCOMPARE(call.args["allow_partial"].toBool(),false);
        agent.finish(call,{{"device","emulator-5556"},{"restore_id","fedcba9876543210fedcba9876543210"},{"results",QVariantList{QVariantMap{{"field","dark_mode"},{"status","applied"},{"value","on"}}}}});
        QVERIFY(enabled("environmentRestore"));QVERIFY(!enabled("environmentApply"));
        invoke("restore");call=agent.calls.last();QCOMPARE(call.op,QString("android.environment.restore"));QCOMPARE(call.args["restore_id"].toString(),QString("fedcba9876543210fedcba9876543210"));QCOMPARE(call.args["device"].toString(),QString("emulator-5556"));
        for(auto &c:agent.calls)if(c.args.contains("device"))QCOMPARE(c.args["device"].toString(),QString("emulator-5556"));
    }
    void unsupported_settings_need_explicit_consent_and_a_new_device_needs_a_new_plan(){
        loadPreset();target();invoke("makePlan");agent.finish(agent.calls.last(),plan({"talkback"}));
        QVERIFY(!enabled("environmentApply"));QVERIFY(object("environmentPartial")->property("visible").toBool());
        object("environmentPartial")->setProperty("checked",true);QCoreApplication::processEvents();QVERIFY(enabled("environmentApply"));
        invoke("apply");QCOMPARE(agent.calls.last().args["allow_partial"].toBool(),true);
        agent.fail(agent.calls.last(),"Android did not apply: dark_mode",{{"restore_id","fedcba9876543210fedcba9876543210"},{"device","emulator-5556"}});
        QVERIFY(enabled("environmentRestore"));QVERIFY(page->property("failed").toBool());
        // Another device invalidates the plan.
        invoke("makePlan");agent.finish(agent.calls.last(),plan());page->setProperty("device","emulator-5554");QCoreApplication::processEvents();
        QVERIFY(!page->property("planIsCurrent").toBool());QVERIFY(!enabled("environmentApply"));
    }
    void a_route_is_read_first_played_on_the_chosen_emulator_only_and_paused_resumed_stopped(){
        loadPreset();
        QVERIFY(!enabled("routePlay"));
        invoke("inspectRoute","/tmp/ride.gpx");
        auto call=agent.calls.last();QCOMPARE(call.op,QString("gpx.inspect"));QCOMPARE(call.args["path"].toString(),QString("/tmp/ride.gpx"));QVERIFY(!call.args.contains("device"));
        agent.finish(call,{{"name","Ring"},{"points",4},{"distance_m",270.5},{"duration_s",60.0},{"timing",QVariantMap{{"timed",true},{"ordering","time"}}}});
        QVERIFY(object("routeSummary")->property("text").toString().contains("Ring"));
        QVERIFY(object("routeSummary")->property("text").toString().contains("0.27 km"));
        // No emulator chosen: nothing plays.
        QVERIFY(!enabled("routePlay"));
        target();
        QTest::qWait(10);
        QVERIFY2(enabled("routePlay"),qPrintable(QString("route=%1 emulator=%2 playing=%3 busy=%4").arg(page->property("route").isValid()).arg(page->property("routeTargetIsEmulator").toBool()).arg(page->property("routePlaying").toBool()).arg(page->property("busy").toInt())));
        object("routeSpeed")->setProperty("text","4");object("routeLoop")->setProperty("checked",true);QCoreApplication::processEvents();
        invoke("playRoute");call=agent.calls.last();
        QCOMPARE(call.op,QString("gpx.play"));QCOMPARE(call.args["device"].toString(),QString("emulator-5556"));QCOMPARE(call.args["path"].toString(),QString("/tmp/ride.gpx"));
        QCOMPARE(call.args["speed"].toDouble(),4.0);QCOMPARE(call.args["loop"].toBool(),true);
        agent.finish(call,{{"job",QVariantMap{{"id","0123456789abcdef0123456789abcdef"},{"state","running"}}},{"device","emulator-5556"},{"baseline",QVariantMap{{"known",false}}}});
        QVERIFY(page->property("routePlaying").toBool());QVERIFY(!enabled("routePlay"));QVERIFY(enabled("routeStop"));QVERIFY(!enabled("routePause"));
        QVERIFY(object("environmentNotice")->property("text").toString().contains("will not be restored"));
        invoke("pollRoute");
        auto status=agent.calls[agent.calls.size()-2];QCOMPARE(status.op,QString("gpx.status"));QCOMPARE(status.args["job_id"].toString(),QString("0123456789abcdef0123456789abcdef"));
        agent.finish(status,{{"job_id","0123456789abcdef0123456789abcdef"},{"progress",QVariantMap{{"state","playing"},{"index",1},{"total",4}}}});
        agent.finish(agent.calls.last(),{{"id","0123456789abcdef0123456789abcdef"},{"state","running"}});
        QVERIFY(enabled("routePause"));QVERIFY(object("routeState")->property("text").toString().contains("2 of 4"));
        invoke("routeSend","pause");call=agent.calls.last();QCOMPARE(call.op,QString("job.send"));QCOMPARE(call.args["command"].toString(),QString("pause"));
        agent.finish(call,{{"sent","pause"}});
        invoke("pollRoute");status=agent.calls[agent.calls.size()-2];agent.finish(status,{{"progress",QVariantMap{{"state","paused"},{"index",1},{"total",4}}}});
        agent.finish(agent.calls.last(),{{"id","0123456789abcdef0123456789abcdef"},{"state","running"}});
        QVERIFY(page->property("routePaused").toBool());QCOMPARE(object("routePause")->property("text").toString(),QString("Resume"));
        invoke("routeSend","resume");QCOMPARE(agent.calls.last().args["command"].toString(),QString("resume"));
        agent.finish(agent.calls.last(),{{"sent","resume"}});
        invoke("routeSend","stop");QCOMPARE(agent.calls.last().args["command"].toString(),QString("stop"));
        agent.finish(agent.calls.last(),{{"sent","stop"}});
        invoke("pollRoute");
        agent.finish(agent.calls[agent.calls.size()-2],{{"progress",QVariantMap{{"state","stopped"},{"index",2},{"total",4}}}});
        agent.finish(agent.calls.last(),{{"id","0123456789abcdef0123456789abcdef"},{"state","succeeded"}});
        QVERIFY(!page->property("routePlaying").toBool());QVERIFY(enabled("routePlay"));
        QVERIFY(object("routeState")->property("text").toString().contains("Stopped"));
        // A phone as the target: playback is refused before any call.
        page->setProperty("device","R5CT");QCoreApplication::processEvents();
        QVERIFY(!page->property("routeTargetIsEmulator").toBool());
    }
    void responsive_layout_data(){QTest::addColumn<int>("width");QTest::addColumn<int>("scale");for(int s:{100,175,200})for(int w:{1380,960,560})QTest::newRow(qPrintable(QString("%1-%2").arg(w).arg(s)))<<w<<s;}
    void responsive_layout(){
        QFETCH(int,width);QFETCH(int,scale);loadPreset();target();invoke("makePlan");agent.finish(agent.calls.last(),plan({"talkback"}));
        style.scale=scale;emit style.changed();window->resize(width,2600);page->setWidth(width);QTest::qWait(60);
        int checked=0;
        for(auto item:page->findChildren<QQuickItem*>()){
            if(!item->isVisible() || (!item->inherits("QQuickTextField") && !item->inherits("QQuickComboBox") && !item->inherits("QQuickButton") && !item->inherits("QQuickCheckBox")))continue;
            auto point=item->mapToItem(page,QPointF());if(point.x()+item->width()>width+1){for(auto p=item;p;p=p->parentItem())qWarning()<<p->metaObject()->className()<<p->objectName()<<p->x()<<p->width()<<p->implicitWidth();}QVERIFY2(item->width()>20,qPrintable(item->objectName()));
            QVERIFY2(point.x()>=-1 && point.x()+item->width()<=width+1,qPrintable(QString("%1 x%2 w%3 > %4").arg(item->objectName()).arg(point.x()).arg(item->width()).arg(width)));++checked;
        }
        QVERIFY2(checked>=15,qPrintable(QString::number(checked)));
        QString capture=qEnvironmentVariable("TOMACCO_ENVIRONMENT_UI_CAPTURE_DIR");
        if(!capture.isEmpty()){QDir().mkpath(capture);window->resize(width,qMin(2600,int(page->implicitHeight())+20));QTest::qWait(30);QVERIFY(window->grabWindow().save(capture+QString("/environment-%1-%2.png").arg(width).arg(scale)));}
    }
};
QTEST_MAIN(EnvironmentPanelTest)
#include "environment_panel_test.moc"
