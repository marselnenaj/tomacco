// Local QML contract fixtures only: no Control, CLI, renderer or devices.
#include <QGuiApplication>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QQuickItem>
#include <QJSValue>
#include <QTemporaryDir>
#include <QImage>
#include <QDir>
#include <QInputMethodEvent>
#include <QtTest>
#include <memory>

class DemoStyle : public QObject {
    Q_OBJECT
    Q_PROPERTY(int uiScale MEMBER scale NOTIFY changed)
    Q_PROPERTY(bool dark MEMBER dark NOTIFY changed)
    Q_PROPERTY(QVariantMap omarchyTheme READ omarchyTheme CONSTANT)
    Q_PROPERTY(bool followOmarchyAccent READ followOmarchyAccent CONSTANT)
public:
    int scale=100;bool dark=true;
    QVariantMap omarchyTheme()const{return {};}
    bool followOmarchyAccent()const{return false;}
    Q_INVOKABLE void revealFile(QString){}
signals:void changed();
};
class DemoAgent : public QObject {
    Q_OBJECT
public:
    struct Call {int id;QString op;QVariantMap args;};
    QList<Call> calls;int serial=0;
    Q_INVOKABLE int request(QString op,QVariantMap args,bool quiet=true){Q_UNUSED(quiet);calls.append({++serial,op,args});return serial;}
    void finish(Call call,QVariantMap result){emit apiFinished(call.id,call.op,{{"ok",true},{"result",result}});QCoreApplication::processEvents();}
    void fail(Call call,QString message){emit apiFinished(call.id,call.op,{{"ok",false},{"error",QVariantMap{{"message",message}}}});QCoreApplication::processEvents();}
signals:void apiFinished(int id,QString op,QVariantMap response);
};
class DemoWorkspaceTest : public QObject {
    Q_OBJECT
    DemoStyle style;DemoAgent agent;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;
    QQuickItem *page=nullptr;QTemporaryDir folder;
    QVariant fromJs(QVariant value){return value.metaType()==QMetaType::fromType<QJSValue>()?value.value<QJSValue>().toVariant():value;}
    QVariantMap draft(){return fromJs(page->property("draft")).toMap();}
    QObject *object(const char *name){return page->findChild<QObject*>(QString::fromLatin1(name));}
    QVariant invoke(const char *name,QVariant a={},QVariant b={},QVariant c={}){
        QVariant result;bool ok=false;
        if(!a.isValid())ok=QMetaObject::invokeMethod(page,name,Q_RETURN_ARG(QVariant,result));
        else if(!b.isValid())ok=QMetaObject::invokeMethod(page,name,Q_RETURN_ARG(QVariant,result),Q_ARG(QVariant,a));
        else if(!c.isValid())ok=QMetaObject::invokeMethod(page,name,Q_RETURN_ARG(QVariant,result),Q_ARG(QVariant,a),Q_ARG(QVariant,b));
        else ok=QMetaObject::invokeMethod(page,name,Q_RETURN_ARG(QVariant,result),Q_ARG(QVariant,a),Q_ARG(QVariant,b),Q_ARG(QVariant,c));
        if(!ok)QTest::qFail(name,__FILE__,__LINE__);QCoreApplication::processEvents();return result;
    }
    QVariantMap pose(double time,double yaw){return {{"time",time},{"yaw",yaw},{"pitch",8},{"roll",0},{"scale",.72},{"x",.5},{"y",.5},{"easing","smooth"}};}
    QVariantMap scene(){return {{"format","tomacco.demo"},{"version",1},{"revision",1},{"title","Fixture demo"},{"source",QVariantMap{{"path","/fixture/source.mp4"},{"width",360},{"height",780},{"duration",8}}},{"clip",QVariantMap{{"start",0}}},{"output",QVariantMap{{"width",1080},{"height",1920},{"duration",8},{"fps",30}}},{"device",QVariantMap{{"model","phone"},{"finish","#242421"},{"bezel",.035},{"thickness",.065},{"corner_radius",.09}}},{"background",QVariantMap{{"style","solid"},{"color","#f4f2ec"},{"color2","#f4f2ec"},{"image",""}}},{"shadow",QVariantMap{{"enabled",true},{"opacity",.22},{"blur",24},{"offset_x",0},{"offset_y",40}}},{"camera",QVariantMap{{"distance",4},{"fov",35}}},{"keyframes",QVariantList{pose(0,-18),pose(4,0),pose(8,18)}}};}
    QVariantMap reply(QVariantMap value,int revision=1){value["revision"]=revision;return {{"path","/fixture/scene.json"},{"scene",value},{"revision",revision}};}
    void load(){invoke("openScene","/fixture/scene.json");QCOMPARE(agent.calls.last().op,QString("demo.scene.get"));agent.finish(agent.calls.last(),reply(scene()));QVERIFY(page->property("loaded").toBool());}
    void ready(){agent.finish(agent.calls.first(),{{"ready",true},{"presets",QStringList{"portrait-orbit","square-float"}},{"models",QStringList{"phone","tablet"}}});}
private slots:
    void init(){
        agent.calls.clear();style.scale=100;style.dark=true;
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);engine->rootContext()->setContextProperty("Agent",&agent);
        QQmlComponent component(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/DemoWorkspace.qml")));
        QVERIFY2(component.isReady(),qPrintable(component.errorString()));
        page=qobject_cast<QQuickItem*>(component.createWithInitialProperties({{"workspace",QVariant::fromValue(&agent)}}));QVERIFY2(page,qPrintable(component.errorString()));
        window=std::make_unique<QQuickWindow>();window->setColor(QColor("#0a0a09"));window->resize(1100,780);page->setParentItem(window->contentItem());page->setSize(window->size());window->show();QTest::qWait(15);
        QCOMPARE(agent.calls.size(),1);QCOMPARE(agent.calls.first().op,QString("demo.capabilities"));
    }
    void cleanup(){window.reset();delete page;page=nullptr;engine.reset();}
    void entry_has_only_local_capability_call(){QCOMPARE(agent.calls.size(),1);QVERIFY(!page->property("loaded").toBool());}
    void create_inspect_and_open_use_shared_ops(){
        ready();object("demoSource")->setProperty("text","/fixture/source.mp4");object("demoScenePath")->setProperty("text","/fixture/new.json");invoke("inspectSource");QCOMPARE(agent.calls.last().op,QString("demo.media.inspect"));agent.finish(agent.calls.last(),{{"path","/fixture/source.mp4"},{"width",360},{"height",780},{"duration",8}});
        invoke("createScene");auto call=agent.calls.last();QCOMPARE(call.op,QString("demo.scene.create"));QCOMPARE(call.args["source"].toString(),QString("/fixture/source.mp4"));QCOMPARE(call.args["preset"].toString(),QString("portrait-orbit"));agent.finish(call,reply(scene()));QVERIFY(page->property("loaded").toBool());QVERIFY(!page->property("dirty").toBool());
    }
    void edits_preserve_all_agent_keyframes_and_revision(){
        ready();load();page->setProperty("selectedPose",1);invoke("setPose","yaw",11);invoke("setField","output","width",1920);QVERIFY(page->property("dirty").toBool());QVERIFY(!object("demoExport")->property("enabled").toBool());
        invoke("saveChanges");auto call=agent.calls.last();QCOMPARE(call.op,QString("demo.scene.update"));QCOMPARE(call.args["if_revision"].toInt(),1);auto changes=call.args["changes"].toMap();auto frames=changes["keyframes"].toList();QCOMPARE(frames.size(),3);QCOMPARE(frames[0].toMap()["yaw"].toInt(),-18);QCOMPARE(frames[1].toMap()["yaw"].toInt(),11);QCOMPARE(frames[2].toMap()["yaw"].toInt(),18);QVERIFY(!changes.contains("source"));
        agent.finish(call,reply(draft(),2));QVERIFY(!page->property("dirty").toBool());QCOMPARE(page->property("revision").toInt(),2);
    }
    void stale_revision_error_keeps_draft_and_newer_edits_survive_save(){
        ready();load();invoke("setField","","title","Unsaved title");invoke("saveChanges");agent.fail(agent.calls.last(),"Scene changed; reload first.");QVERIFY(page->property("dirty").toBool());QCOMPARE(draft()["title"].toString(),QString("Unsaved title"));QVERIFY(!page->property("saving").toBool());
        invoke("saveChanges");auto call=agent.calls.last();auto earlier=draft();invoke("setPose","yaw",42);agent.finish(call,reply(earlier,2));QCOMPARE(page->property("revision").toInt(),2);QVERIFY(page->property("dirty").toBool());QCOMPARE(draft()["keyframes"].toList()[0].toMap()["yaw"].toInt(),42);
    }
    void dirty_draft_blocks_open_and_late_reload_cannot_replace_edits(){
        ready();load();invoke("openScene","/fixture/scene.json");auto pending=agent.calls.last();invoke("setField","","title","Keep my draft");agent.finish(pending,reply(scene(),4));QCOMPARE(draft()["title"].toString(),QString("Keep my draft"));QCOMPARE(page->property("revision").toInt(),1);int count=agent.calls.size();invoke("openScene","/fixture/other.json");QCOMPARE(agent.calls.size(),count);
    }
    void add_remove_poses_preserves_middle_pose(){
        ready();load();invoke("addPose");QCOMPARE(draft()["keyframes"].toList().size(),4);invoke("removePose");auto frames=draft()["keyframes"].toList();QCOMPARE(frames.size(),3);QCOMPARE(frames[1].toMap()["time"].toInt(),4);
    }
    void preview_only_displays_after_success_and_export_reports_failure(){
        ready();load();const QString path=folder.filePath("preview.png");QImage image(200,300,QImage::Format_RGB32);image.fill(Qt::red);QVERIFY(image.save(path));
        invoke("render","preview",path);auto call=agent.calls.last();QCOMPARE(call.op,QString("demo.preview"));QCOMPARE(call.args["if_revision"].toInt(),1);agent.finish(call,{{"id","fixture-job"},{"state","queued"}});QCOMPARE(agent.calls.last().op,QString("job.get"));QVERIFY(page->property("previewPath").toString().isEmpty());agent.finish(agent.calls.last(),{{"id","fixture-job"},{"state","succeeded"},{"result",QVariantMap{{"artifacts",QVariantList{QVariantMap{{"path",path}}}}}}});QCOMPARE(page->property("previewPath").toString(),path);
        invoke("render","export",folder.filePath("output.mp4"));call=agent.calls.last();QCOMPARE(call.op,QString("demo.export"));agent.finish(call,{{"id","export-job"},{"state","running"}});agent.finish(agent.calls.last(),{{"id","export-job"},{"state","failed"},{"result",QVariantMap{{"error","Fixture codec failure"}}}});QVERIFY(page->property("failed").toBool());QVERIFY(page->property("completedOutput").toString().isEmpty());QCOMPARE(page->property("previewPath").toString(),path);
    }
    void export_success_keeps_confirmed_output_and_poster(){
        ready();load();QString path=folder.filePath("demo.mp4"),poster=path+".poster.png";QImage image(320,240,QImage::Format_RGB32);image.fill(Qt::blue);QVERIFY(image.save(poster));invoke("render","export",path);agent.finish(agent.calls.last(),{{"id","finished-export"},{"state","queued"}});agent.finish(agent.calls.last(),{{"id","finished-export"},{"state","succeeded"},{"result",QVariantMap{{"artifacts",QVariantList{QVariantMap{{"path",path}},QVariantMap{{"path",poster}},QVariantMap{{"path",path+".manifest.json"}}}}}}});QCOMPARE(page->property("completedOutput").toString(),path);QCOMPARE(page->property("previewPath").toString(),poster);QCOMPARE(page->property("completedRevision").toInt(),1);QVERIFY(!page->property("rendering").toBool());
    }
    void focused_field_is_dirty_before_blur_and_invalid_number_blocks_render(){
        ready();load();auto input=qobject_cast<QQuickItem*>(object("demoYawInput"));QVERIFY(input);input->forceActiveFocus();QVERIFY(QMetaObject::invokeMethod(input,"selectAll"));QInputMethodEvent edit;edit.setCommitString("47");QCoreApplication::sendEvent(input,&edit);QVERIFY(input->hasActiveFocus());QVERIFY(page->property("dirty").toBool());QCOMPARE(draft()["keyframes"].toList()[0].toMap()["yaw"].toInt(),47);
        agent.finish(agent.calls.first(),{{"ready",true}});invoke("numberField","output","duration",QString());QVERIFY(page->property("dirty").toBool());QVERIFY(!page->property("canRender").toBool());int count=agent.calls.size();invoke("saveChanges");QCOMPARE(agent.calls.size(),count);invoke("numberField","output","duration","8");invoke("saveChanges");QCOMPARE(agent.calls.last().op,QString("demo.scene.update"));
    }
    void capabilities_reply_still_applies_after_scene_request(){load();ready();QVERIFY(page->property("canRender").toBool());}
    void responsive_layout_data(){QTest::addColumn<int>("width");QTest::addColumn<int>("height");QTest::addColumn<int>("scale");for(int s:{100,175,200})for(auto size:{QSize(1380,970),QSize(960,680),QSize(560,680)})QTest::newRow(qPrintable(QString("%1x%2-%3").arg(size.width()).arg(size.height()).arg(s)))<<size.width()<<size.height()<<s;}
    void responsive_layout(){
        QFETCH(int,width);QFETCH(int,height);QFETCH(int,scale);ready();load();style.scale=scale;emit style.changed();window->resize(width,height);page->setSize(window->size());QTest::qWait(60);
        auto controls=page->findChildren<QQuickItem*>();int checked=0;
        for(auto item:controls){if(!item->isVisible() || (!item->inherits("QQuickTextField") && !item->inherits("QQuickComboBox") && !item->inherits("QQuickButton")))continue;auto point=item->mapToItem(page,QPointF());QVERIFY2(item->width()>20,qPrintable(item->objectName()));QVERIFY2(point.x()>=-1 && point.x()+item->width()<=width+1,qPrintable(QString("%1 x%2 width%3 outside%4").arg(item->objectName()).arg(point.x()).arg(item->width()).arg(width)));++checked;}
        QVERIFY(checked>25);
        QString capture=qEnvironmentVariable("TOMACCO_DEMO_UI_CAPTURE_DIR");if(!capture.isEmpty()){QDir().mkpath(capture);QVERIFY(window->grabWindow().save(capture+QString("/demo-%1-%2.png").arg(width).arg(scale)));auto flick=page->property("contentItem").value<QObject*>();QVERIFY(flick);double maximum=flick->property("contentHeight").toDouble()-height;QVERIFY(maximum>0);for(int stage=1;stage<=3;stage++){flick->setProperty("contentY",maximum*stage/3);QTest::qWait(30);QVERIFY(window->grabWindow().save(capture+QString("/demo-%1-%2-scroll%3.png").arg(width).arg(scale).arg(stage)));}}
    }
};
QTEST_MAIN(DemoWorkspaceTest)
#include "demo_workspace_test.moc"
