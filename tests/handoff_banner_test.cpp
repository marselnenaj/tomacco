// Local QML contract fixtures only: no Control, CLI or devices. The fake agent
// records every typed call the handoff model and banner make and answers them.
#include <QGuiApplication>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QQuickItem>
#include <QtTest>
#include <memory>

class FakeStyle : public QObject {
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
class FakeAgent : public QObject {
    Q_OBJECT
public:
    struct Call {int id;QString op;QVariantMap args;};
    QList<Call> calls;int serial=0;
    Q_INVOKABLE int request(QString op,QVariantMap args){calls.append({++serial,op,args});return serial;}
    void finish(Call call,QVariantMap result){emit apiFinished(call.id,call.op,{{"ok",true},{"result",result}});QCoreApplication::processEvents();}
    void fail(Call call,QString message){emit apiFinished(call.id,call.op,{{"ok",false},{"error",QVariantMap{{"message",message},{"code","operation_not_found"}}}});QCoreApplication::processEvents();}
    QStringList ops()const{QStringList out;for(auto &c:calls)out<<c.op;return out;}
signals:void apiFinished(int id,QString op,QVariantMap response);
};

class HandoffBannerTest : public QObject {
    Q_OBJECT
    FakeStyle style;FakeAgent agent;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;QQuickItem *model=nullptr;QQuickItem *banner=nullptr;
    int arrivals=0;
    QObject *object(const char *name){return banner->findChild<QObject*>(QString::fromLatin1(name));}
    QVariantMap waiting(QString id,QString kind,QString reason,QString instructions=""){
        return {{"id",id},{"state","waiting"},{"kind",kind},{"reason",reason},{"instructions",instructions},{"session",""},{"created",1.0}};
    }
    void answerList(QVariantList list){
        QVERIFY(!agent.calls.isEmpty());
        auto call=agent.calls.last();QCOMPARE(call.op,QString("handoff.list"));QCOMPARE(call.args["state"].toString(),QString("waiting"));
        agent.finish(call,{{"handoffs",list},{"count",list.size()},{"truncated",false},{"problems",QVariantList{}}});
    }
private slots:
    void init(){
        agent.calls.clear();style.scale=100;arrivals=0;
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);engine->rootContext()->setContextProperty("Agent",&agent);
        QQmlComponent modelComponent(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/HandoffModel.qml")));
        QVERIFY2(modelComponent.isReady(),qPrintable(modelComponent.errorString()));
        model=qobject_cast<QQuickItem*>(modelComponent.createWithInitialProperties({{"polling",false}}));QVERIFY2(model,qPrintable(modelComponent.errorString()));
        QObject::connect(model,SIGNAL(arrived(QVariant)),this,SLOT(noteArrival()));
        QQmlComponent bannerComponent(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/HandoffBanner.qml")));
        QVERIFY2(bannerComponent.isReady(),qPrintable(bannerComponent.errorString()));
        banner=qobject_cast<QQuickItem*>(bannerComponent.createWithInitialProperties({{"model",QVariant::fromValue(model)}}));QVERIFY2(banner,qPrintable(bannerComponent.errorString()));
        window=std::make_unique<QQuickWindow>();window->resize(1100,300);banner->setParentItem(window->contentItem());banner->setWidth(window->width());window->show();QTest::qWait(15);
    }
    void cleanup(){window.reset();delete banner;banner=nullptr;delete model;model=nullptr;engine.reset();}
    void noteArrival(){++arrivals;}
    void nothing_waiting_shows_no_banner_and_an_old_cli_stays_quiet(){
        QVERIFY(!banner->isVisible());
        QMetaObject::invokeMethod(model,"refresh");QCoreApplication::processEvents();
        answerList({});
        QVERIFY(!banner->isVisible());QCOMPARE(banner->implicitHeight(),0.0);
        QMetaObject::invokeMethod(model,"refresh");QCoreApplication::processEvents();
        agent.fail(agent.calls.last(),"Unknown operation: handoff.list");
        QVERIFY(!banner->isVisible());QCOMPARE(model->property("error").toString(),QString());
    }
    void a_waiting_handoff_shows_why_and_done_completes_exactly_that_one(){
        QMetaObject::invokeMethod(model,"refresh");QCoreApplication::processEvents();
        answerList({waiting("0123456789abcdef0123456789abcdef","purchase","Confirm the purchase with Face ID","Look at the phone"),
                    waiting("fedcba9876543210fedcba9876543210","passcode","Unlock the phone")});
        QVERIFY(banner->isVisible());QVERIFY(banner->implicitHeight()>0);
        QCOMPARE(arrivals,2);
        QString title=object("handoffBannerTitle")->property("text").toString();
        QVERIFY2(title.contains("Purchase") && title.contains("1 more"),qPrintable(title));
        QVERIFY(object("handoffBannerReason")->property("text").toString().contains("Confirm the purchase with Face ID"));
        QVERIFY(object("handoffBannerReason")->property("text").toString().contains("Look at the phone"));
        QVERIFY(object("handoffBannerNote")->property("text").toString().contains("never asks for your passcode"));
        QMetaObject::invokeMethod(object("handoffBannerDone"),"clicked");QCoreApplication::processEvents();
        auto call=agent.calls.last();QCOMPARE(call.op,QString("handoff.complete"));QCOMPARE(call.args["id"].toString(),QString("0123456789abcdef0123456789abcdef"));
        agent.finish(call,{{"already",false}});
        // Done is followed by a fresh list; the second request is now first.
        answerList({waiting("fedcba9876543210fedcba9876543210","passcode","Unlock the phone")});
        QCOMPARE(arrivals,2);
        QVERIFY(object("handoffBannerTitle")->property("text").toString().contains("Passcode"));
        QMetaObject::invokeMethod(object("handoffBannerCancel"),"clicked");QCoreApplication::processEvents();
        call=agent.calls.last();QCOMPARE(call.op,QString("handoff.cancel"));QCOMPARE(call.args["id"].toString(),QString("fedcba9876543210fedcba9876543210"));
        agent.finish(call,{{"already",false}});answerList({});
        QVERIFY(!banner->isVisible());
        for(auto &c:agent.calls)QVERIFY2(c.op=="handoff.list" || c.op=="handoff.complete" || c.op=="handoff.cancel",qPrintable(c.op));
    }
    void responsive_layout_data(){QTest::addColumn<int>("width");QTest::addColumn<int>("scale");for(int s:{100,175,200})for(int w:{1380,560,360})QTest::newRow(qPrintable(QString("%1-%2").arg(w).arg(s)))<<w<<s;}
    void responsive_layout(){
        QFETCH(int,width);QFETCH(int,scale);
        QMetaObject::invokeMethod(model,"refresh");QCoreApplication::processEvents();
        answerList({waiting("0123456789abcdef0123456789abcdef","sign_in","Sign in to the App Store with your own account so the download can continue","Open the App Store sheet on the phone and sign in there")});
        style.scale=scale;emit style.changed();window->resize(width,600);banner->setWidth(width);QTest::qWait(60);
        int checked=0;
        for(auto item:banner->findChildren<QQuickItem*>()){
            if(!item->isVisible() || (!item->inherits("QQuickButton") && !item->inherits("QQuickText")))continue;
            auto point=item->mapToItem(banner,QPointF());
            QVERIFY2(point.x()>=-1 && point.x()+item->width()<=width+1,qPrintable(QString("%1 x%2 w%3 > %4").arg(item->objectName()).arg(point.x()).arg(item->width()).arg(width)));
            QVERIFY2(point.y()+item->height()<=banner->implicitHeight()+1,qPrintable(item->objectName()));
            ++checked;
        }
        QVERIFY2(checked>=5,qPrintable(QString::number(checked)));
        QVERIFY(object("handoffBannerDone")->property("width").toDouble()>20);
    }
};
QTEST_MAIN(HandoffBannerTest)
#include "handoff_banner_test.moc"
