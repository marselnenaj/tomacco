// The phone's bottom navigation, as a local QML contract: no Control, CLI or
// devices. It loads qml/MobileNav.qml with the workspace's navigation list and
// checks what a phone needs from it: every primary destination in the bar with
// a label and a target of at least 48 logical px, the rest behind More, and
// navigation by tap at 100 % and 200 % interface size, 360 and 412 px wide.
#include <QGuiApplication>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlComponent>
#include <QQmlExpression>
#include <QQuickWindow>
#include <QQuickItem>
#include <QSignalSpy>
#include <QtTest>
#include <memory>

class NavStyle : public QObject {
    Q_OBJECT
    Q_PROPERTY(int uiScale MEMBER scale NOTIFY changed)
    Q_PROPERTY(bool dark MEMBER dark NOTIFY changed)
    Q_PROPERTY(QVariantMap omarchyTheme READ omarchyTheme CONSTANT)
    Q_PROPERTY(bool followOmarchyAccent READ followOmarchyAccent CONSTANT)
public:
    int scale=100;bool dark=true;
    QVariantMap omarchyTheme()const{return {};}
    bool followOmarchyAccent()const{return false;}
    void setScale(int value){scale=value;emit changed();}
signals:void changed();
};

class MobileNavTest : public QObject {
    Q_OBJECT
    NavStyle style;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;
    QQuickItem *nav=nullptr;
    // The same list NativeWorkspace hands the rail: id, label, icon, key.
    static QVariantList navigation() {
        QVariantList out;
        const QList<QStringList> entries{{"runtime","Live device","phone","1"},{"files","Files","files","2"},{"jobs","Builds & jobs","jobs","3"},{"devices","Devices","devices","4"},{"project","Overview","project","5"},
                                         {"debug","Debugger","debug","6"},{"testing","UI tests","testing","7"},{"profiling","Performance","profiling","8"},{"release","Release","release","9"}};
        for(const auto &e:entries)out.append(QVariantMap{{"id",e[0]},{"label",e[1]},{"icon",e[2]},{"key",e[3]}});
        return out;
    }
    // Repeater delegates have no QObject parent: walk the visual tree, as the desktop UI test does.
    static QQuickItem *visualItem(QQuickItem *parent,const QString &name){
        if(parent->objectName()==name)return parent;
        for(auto *child:parent->childItems())if(auto *found=visualItem(child,name))return found;
        return nullptr;
    }
    QQuickItem *item(const QString &name){return visualItem(window->contentItem(),name);}
    // The More sheet is a Popup (a QObject under the bar); its rows are items in the window's overlay.
    QObject *object(const QString &name){
        if(auto *own=nav->findChild<QObject*>(name))return own;
        return visualItem(window->contentItem(),name);
    }
    QQuickItem *overlayItem(const QString &name){return visualItem(window->contentItem(),name);}
    void tap(QQuickItem *target){
        const auto point=target->mapToScene(QPointF(target->width()/2,target->height()/2)).toPoint();
        QTest::mouseClick(window.get(),Qt::LeftButton,Qt::NoModifier,point);QCoreApplication::processEvents();
    }
    void layout(int width,int scale){
        style.setScale(scale);
        window->resize(width,852);nav->setWidth(width);nav->setHeight(nav->implicitHeight());nav->setY(852-nav->height());
        QCoreApplication::processEvents();QTest::qWait(30);
    }
private slots:
    void init(){
        style.scale=100;style.dark=true;
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);
        QQmlComponent component(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/MobileNav.qml")));
        QVERIFY2(component.isReady(),qPrintable(component.errorString()));
        nav=qobject_cast<QQuickItem*>(component.createWithInitialProperties({{"navigation",navigation()},{"current","project"}}));
        QVERIFY2(nav,qPrintable(component.errorString()));
        window=std::make_unique<QQuickWindow>();window->setColor(QColor("#0e0e0c"));window->resize(393,852);
        nav->setParentItem(window->contentItem());window->show();QVERIFY(QTest::qWaitForWindowExposed(window.get()));
        layout(393,100);
    }
    void cleanup(){window.reset();delete nav;nav=nullptr;engine.reset();}

    void primary_destinations_and_more_fit_with_touch_targets_data(){
        QTest::addColumn<int>("width");QTest::addColumn<int>("scale");
        QTest::newRow("360 at 100%")<<360<<100;QTest::newRow("393 at 100%")<<393<<100;QTest::newRow("412 at 100%")<<412<<100;
        QTest::newRow("393 at 175%")<<393<<175;QTest::newRow("412 at 200%")<<412<<200;
    }
    void primary_destinations_and_more_fit_with_touch_targets(){
        QFETCH(int,width);QFETCH(int,scale);layout(width,scale);
        const double unit=scale/100.0;
        QVERIFY2(nav->height()>=48*unit,qPrintable(QString::number(nav->height())));
        for(const QString &id:{"project","devices","jobs","files","more"}){
            auto *entry=item("mobileNav-"+id);QVERIFY2(entry,qPrintable(id));
            const auto bounds=entry->mapRectToScene(entry->boundingRect());
            // A target stays at least 48 px wide on the screen whatever the interface size; its height follows the scale.
            QVERIFY2(bounds.width()>=48 && bounds.height()>=48*unit,qPrintable(id+" "+QString::number(bounds.width())+"x"+QString::number(bounds.height())));
            QVERIFY2(bounds.left()>=0 && bounds.right()<=width+0.5,qPrintable(id));
            // The label is always shown and never wider than its target.
            bool labelled=false;
            for(auto *child:entry->findChildren<QQuickItem*>()){
                if(child->property("text").isValid() && !child->property("text").toString().isEmpty()){labelled=true;QVERIFY2(child->width()<=entry->width()+0.5,qPrintable(id));}
            }
            QVERIFY2(labelled,qPrintable(id));
            auto *caption=item("mobileNavLabel-"+id);QVERIFY2(caption,qPrintable(id));
            QVERIFY2(!caption->property("truncated").toBool(),qPrintable(id+": "+caption->property("text").toString()));
            const auto captionBounds=caption->mapRectToScene(caption->boundingRect());
            QVERIFY2(captionBounds.top()>=bounds.top()-0.5 && captionBounds.bottom()<=bounds.bottom()+0.5,qPrintable(id));
        }
        QCOMPARE(item("mobileNavLabel-jobs")->property("text").toString(),QString("Jobs"));
        QCOMPARE(item("mobileNavLabel-project")->property("text").toString(),QString("Project"));
        // The desktop rail's other sections are not in the bar: they sit behind More.
        QVERIFY(!item("mobileNav-debug"));QVERIFY(!item("mobileNav-release"));
    }
    void current_section_is_marked_and_taps_navigate(){
        QVERIFY(item("mobileNav-project"));QVERIFY(item("mobileNav-jobs"));
        QVERIFY(item("mobileNav-project")->property("active").toBool());
        QVERIFY(!item("mobileNav-jobs")->property("active").toBool());
        QSignalSpy navigated(nav,SIGNAL(navigate(QString)));
        tap(item("mobileNav-jobs"));
        QTRY_COMPARE(navigated.count(),1);QCOMPARE(navigated.first().first().toString(),QString("jobs"));
        nav->setProperty("current","jobs");
        QVERIFY(item("mobileNav-jobs")->property("active").toBool());QVERIFY(!item("mobileNav-project")->property("active").toBool());
    }
    void more_sheet_lists_the_rest_and_navigates(){
        auto *more=object("mobileNavMore");QVERIFY(more);QVERIFY(!more->property("visible").toBool());
        tap(item("mobileNav-more"));
        QTRY_VERIFY(more->property("visible").toBool());
        for(const QString &id:{"runtime","debug","testing","profiling","release"}){
            auto *row=overlayItem("mobileNavMore-"+id);QVERIFY2(row,qPrintable(id));
            QVERIFY2(row->height()>=48,qPrintable(id));
        }
        QVERIFY(!object("mobileNavMore-project"));
        QSignalSpy navigated(nav,SIGNAL(navigate(QString)));
        auto *release=overlayItem("mobileNavMore-release");QVERIFY(release);
        tap(release);
        QTRY_COMPARE(navigated.count(),1);QCOMPARE(navigated.first().first().toString(),QString("release"));
        QTRY_VERIFY(!more->property("visible").toBool());
        // A section behind More marks More as the active destination.
        nav->setProperty("current","release");
        QVERIFY(item("mobileNav-more")->property("active").toBool());QVERIFY(!item("mobileNav-project")->property("active").toBool());
    }
    void back_closes_the_sheet_without_navigating(){
        auto *more=object("mobileNavMore");QVERIFY(more);
        tap(item("mobileNav-more"));QTRY_VERIFY(more->property("visible").toBool());
        QSignalSpy navigated(nav,SIGNAL(navigate(QString)));
        QTest::keyClick(window.get(),Qt::Key_Escape);
        QTRY_VERIFY(!more->property("visible").toBool());QCOMPARE(navigated.count(),0);
    }
};

int main(int argc,char **argv){
    qputenv("QT_QPA_PLATFORM","offscreen");qputenv("QT_QUICK_BACKEND","software");qputenv("QT_QUICK_CONTROLS_STYLE","Basic");
    QGuiApplication app(argc,argv);MobileNavTest test;return QTest::qExec(&test,argc,argv);
}
#include "mobile_nav_test.moc"
