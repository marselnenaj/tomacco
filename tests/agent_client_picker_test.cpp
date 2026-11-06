// Local QML contract fixtures only: no Control, CLI, client program or device.
// The fake agent records each agentClients() call the picker makes and answers
// with reports shaped like `tomaccoctl setup agents --json`.
#include <QGuiApplication>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QQuickItem>
#include <QClipboard>
#include <QtTest>
#include <memory>

class FakeStyle : public QObject {
    Q_OBJECT
    Q_PROPERTY(int uiScale MEMBER scale NOTIFY changed)
    Q_PROPERTY(bool dark MEMBER dark NOTIFY changed)
    Q_PROPERTY(QVariantMap omarchyTheme READ omarchyTheme CONSTANT)
    Q_PROPERTY(bool followOmarchyAccent READ followOmarchyAccent CONSTANT)
public:
    int scale=100;bool dark=false;QString copied;
    QVariantMap omarchyTheme()const{return {};}
    bool followOmarchyAccent()const{return false;}
    Q_INVOKABLE void copy(const QString &text){copied=text;}
signals:void changed();
};
class FakeAgent : public QObject {
    Q_OBJECT
public:
    QList<QPair<QString,QString>> calls;
    Q_INVOKABLE bool agentClients(const QString &action,const QString &client){calls.append({action,client});return true;}
    void answer(const QVariantMap &report,const QString &error={}){auto c=calls.last();emit agentClientsFinished(c.first,c.second,report,error);QCoreApplication::processEvents();}
signals:void agentClientsFinished(const QString &action,const QString &client,const QVariantMap &report,const QString &error);
};

static QVariantMap client(QString id,QString name,QString state,bool found,QString command,QString snippet,QString detail={},QString file={}){
    return {{"id",id},{"name",name},{"state",state},{"found",found},{"command",command.isEmpty()?QVariant():QVariant(command)},
            {"snippet",snippet},{"detail",detail.isEmpty()?QVariant():QVariant(detail)},{"file",file.isEmpty()?QVariant():QVariant(file)},
            {"notes",QVariantList{}},{"connected",state=="connected"}};
}
static QVariantMap statusReport(){
    return {{"action","status"},{"program","/opt/tomacco/tomaccoctl"},{"clients",QVariantList{
        client("claude-code","Claude Code","connected",true,"claude mcp add --transport stdio --scope user tomacco -- /opt/tomacco/tomaccoctl mcp",""),
        client("gemini-cli","Gemini CLI","disabled",true,"","\"mcpServers\": {\"tomacco\": {\"command\": \"/opt/tomacco/tomaccoctl\"}}",{},"/h/.gemini/settings.json"),
        client("cursor","Cursor","conflict",true,"","\"mcpServers\": {}","tomacco already names /usr/bin/other","/h/.cursor/mcp.json"),
        client("opencode","OpenCode","unknown",true,"","{\"mcp\":{}}","layout could not be told","/h/.config/opencode/opencode.json"),
        client("hermes","Hermes Agent","not_connected",true,"hermes mcp add tomacco --command /opt/tomacco/tomaccoctl --args mcp",""),
        client("zed","Zed","absent",false,"","\"context_servers\": {}",{},"/h/.config/zed/settings.json")}}};
}

class AgentClientPickerTest : public QObject {
    Q_OBJECT
    FakeStyle style;FakeAgent agent;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;QQuickItem *picker=nullptr;
    QQuickItem *find(const QString &name){
        QList<QQuickItem*> stack{picker};
        while(!stack.isEmpty()){auto *i=stack.takeFirst();if(i->objectName()==name)return i;stack+=i->childItems();}
        return nullptr;
    }
    bool shown(const QString &name){auto *i=find(name);return i && i->isVisible();}
    void click(const QString &name){QTest::qWait(20);auto *i=find(name);QVERIFY2(i,qPrintable(name));QVERIFY2(i->isVisible(),qPrintable(name));
        QTest::mouseClick(window.get(),Qt::LeftButton,{},i->mapToScene(QPointF(i->width()/2,i->height()/2)).toPoint());QCoreApplication::processEvents();}
    void select(const QString &id){click("agentClient-"+id);QCOMPARE(picker->property("selected").toString(),id);}
private slots:
    void init(){
        agent.calls.clear();style.copied.clear();
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);engine->rootContext()->setContextProperty("Agent",&agent);
        QQmlComponent component(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/AgentClientPicker.qml")));
        QVERIFY2(component.isReady(),qPrintable(component.errorString()));
        picker=qobject_cast<QQuickItem*>(component.create());QVERIFY2(picker,qPrintable(component.errorString()));
        window=std::make_unique<QQuickWindow>();window->resize(900,900);picker->setParentItem(window->contentItem());picker->setWidth(880);window->show();QTest::qWait(15);
    }
    void cleanup(){window.reset();delete picker;picker=nullptr;engine.reset();}
    void status_only_reads_and_each_client_shows_its_own_state(){
        QCOMPARE(agent.calls.size(),1);QCOMPARE(agent.calls[0].first,QString("status"));QCOMPARE(agent.calls[0].second,QString());
        QVERIFY(shown("agentClientsLoading"));
        agent.answer(statusReport());
        QVERIFY(!shown("agentClientsLoading"));
        QCOMPARE(picker->property("selected").toString(),QString("claude-code")); // the connected one first
        QVERIFY(shown("agentClientRemove"));QVERIFY(!shown("agentClientSetUp"));
        click("agentClientCopy");QVERIFY(style.copied.startsWith("claude mcp add"));
        // Disabled: the person's choice stays; text to copy, no setup.
        select("gemini-cli");QVERIFY(!shown("agentClientSetUp"));QVERIFY(!shown("agentClientRemove"));
        QVERIFY(find("agentClientExplanation")->property("text").toString().contains("switched off"));
        click("agentClientCopy");QVERIFY(style.copied.contains("mcpServers"));
        // Conflict and unknown layout: left alone, the detail says why.
        select("cursor");QVERIFY(!shown("agentClientSetUp"));QVERIFY(shown("agentClientDetailText"));
        QCOMPARE(find("agentClientDetailText")->property("text").toString(),QString("tomacco already names /usr/bin/other"));
        select("opencode");QVERIFY(!shown("agentClientSetUp"));
        // Not installed: nothing to set up, the text still for another computer.
        select("zed");QVERIFY(!shown("agentClientSetUp"));QVERIFY(shown("agentClientCopy"));
        QCOMPARE(agent.calls.size(),1); // looking wrote nothing
    }
    void connect_runs_setup_for_that_client_only_then_checks_again(){
        agent.answer(statusReport());
        select("hermes");QVERIFY(shown("agentClientSetUp"));
        click("agentClientSetUp");
        QCOMPARE(agent.calls.last(),qMakePair(QString("setup"),QString("hermes")));
        QVERIFY(!find("agentClientSetUp")->isEnabled());
        auto added=client("hermes","Hermes Agent","added",true,"","");added["backup"]="/h/.hermes/config.yaml.tomacco-backup";
        agent.answer({{"action","setup"},{"clients",QVariantList{added}}});
        QVERIFY(find("agentClientOutcome")->property("text").toString().contains("config.yaml.tomacco-backup"));
        QCOMPARE(agent.calls.last(),qMakePair(QString("status"),QString()));
    }
    void a_failed_setup_and_a_failed_program_are_errors_not_success(){
        agent.answer(statusReport());
        select("hermes");click("agentClientSetUp");
        agent.answer({{"clients",QVariantList{client("hermes","Hermes Agent","failed",true,"","","config.yaml is not valid YAML")}}});
        QCOMPARE(picker->property("error").toString(),QString("config.yaml is not valid YAML"));QVERIFY(shown("agentClientsError"));
        QVERIFY(!shown("agentClientOutcome"));
        picker->setProperty("error",QString());QVERIFY(QMetaObject::invokeMethod(picker,"refresh",Q_ARG(QVariant,false)));QCOMPARE(agent.calls.last().first,QString("status"));
        agent.answer({},"tomaccoctl could not be started");
        QCOMPARE(picker->property("error").toString(),QString("tomaccoctl could not be started"));
    }
    void remove_names_the_client_and_narrow_widths_stack(){
        agent.answer(statusReport());
        click("agentClientRemove");
        QCOMPARE(agent.calls.last(),qMakePair(QString("remove"),QString("claude-code")));
        agent.answer({{"clients",QVariantList{client("claude-code","Claude Code","removed",true,"","")}}});
        QVERIFY(find("agentClientOutcome")->property("text").toString().contains("removed"));
        agent.answer(statusReport());
        // A phone width (360 logical px, 200 % scale): list above details, nothing wider than the picker.
        style.scale=200;emit style.changed();picker->setWidth(360);QTest::qWait(20);
        QVERIFY(picker->property("stacked").toBool());
        for(const char *name:{"agentClientList","agentClientDetail","agentClientCopyText","agentClientCopy"}){
            auto *i=find(QString::fromLatin1(name));QVERIFY2(i&&i->isVisible(),name);
            const auto r=i->mapRectToItem(picker,QRectF(0,0,i->width(),i->height()));
            QVERIFY2(r.left()>=-0.5 && r.right()<=picker->width()+0.5,qPrintable(QString("%1 %2..%3").arg(name).arg(r.left()).arg(r.right())));
        }
        auto *row=find("agentClient-claude-code");QVERIFY(row->height()>=56);
    }
};

int main(int argc,char **argv){
    QGuiApplication app(argc,argv);
    AgentClientPickerTest test;
    return QTest::qExec(&test,argc,argv);
}
#include "agent_client_picker_test.moc"
