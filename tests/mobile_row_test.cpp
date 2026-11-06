// The phone list row, as a local QML contract: no Control, CLI or devices.
// It loads qml/MobileRow.qml and checks the Bogen "Mobile / Row" rules: a
// target of at least 56 logical px that grows with wrapped text, title and
// detail that wrap instead of being cut, the meta text at most 40 % wide on
// the title line, and a tap that reaches the row, at 100 % and 200 %.
#include <QGuiApplication>
#include <QQmlEngine>
#include <QQmlContext>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QQuickItem>
#include <QSignalSpy>
#include <QtTest>
#include <memory>

class RowStyle : public QObject {
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

class MobileRowTest : public QObject {
    Q_OBJECT
    RowStyle style;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;
    QQuickItem *row=nullptr;
    static QQuickItem *visualItem(QQuickItem *parent,const QString &name){
        if(parent->objectName()==name)return parent;
        for(auto *child:parent->childItems())if(auto *found=visualItem(child,name))return found;
        return nullptr;
    }
    QQuickItem *part(const QString &name){return visualItem(row,name);}
    void make(const QVariantMap &properties,int width=393,int scale=100){
        style.setScale(scale);
        QQmlComponent component(engine.get(),QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/MobileRow.qml")));
        QVERIFY2(component.isReady(),qPrintable(component.errorString()));
        row=qobject_cast<QQuickItem*>(component.createWithInitialProperties(properties));
        QVERIFY2(row,qPrintable(component.errorString()));
        window->resize(width,852);row->setParentItem(window->contentItem());row->setWidth(width);
        QCoreApplication::processEvents();QTest::qWait(30);
    }
private slots:
    void init(){
        style.scale=100;style.dark=true;
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);
        window=std::make_unique<QQuickWindow>();window->setColor(QColor("#0e0e0c"));window->resize(393,852);window->show();
        QVERIFY(QTest::qWaitForWindowExposed(window.get()));
    }
    void cleanup(){delete row;row=nullptr;window.reset();engine.reset();}

    void short_row_is_one_touch_target_with_title_detail_and_meta(){
        make({{"title","Beispiel-App"},{"detail","Kotlin · Android · Build erfolgreich"},{"meta","vor 2 h"}});
        QVERIFY2(row->height()>=56,qPrintable(QString::number(row->height())));
        auto *title=part("rowTitle");auto *detail=part("rowDetail");auto *meta=part("rowMeta");auto *dot=part("rowDot");auto *chevron=part("rowChevron");
        QVERIFY(title);QVERIFY(detail);QVERIFY(meta);QVERIFY(dot);QVERIFY(chevron);
        QVERIFY(title->isVisible() && detail->isVisible() && meta->isVisible() && dot->isVisible() && chevron->isVisible());
        // Title 16 semibold, detail 14, meta 13: three roles, not three bold lines.
        QCOMPARE(title->property("font").value<QFont>().pixelSize(),16);QVERIFY(title->property("font").value<QFont>().weight()>=QFont::DemiBold);
        QCOMPARE(detail->property("font").value<QFont>().pixelSize(),14);QCOMPARE(detail->property("font").value<QFont>().weight(),QFont::Normal);
        QCOMPARE(meta->property("font").value<QFont>().pixelSize(),13);
        // The meta text and the dot sit on the title line, not centred on a two-line row.
        QVERIFY(qAbs(meta->mapToScene(QPointF(0,0)).y()-title->mapToScene(QPointF(0,0)).y())<=4);
        QVERIFY(dot->mapToScene(QPointF(0,0)).y()<title->mapToScene(QPointF(0,0)).y()+title->height());
        // Nothing sticks out of the row's width.
        for(auto *p:{title,detail,meta,chevron}){const auto b=p->mapRectToScene(p->boundingRect());QVERIFY2(b.left()>=0 && b.right()<=row->width()+0.5,qPrintable(p->objectName()));}
    }
    void long_texts_wrap_and_the_row_grows(){
        make({{"title","Beispiel-App"},{"detail","kurz"},{"meta","jetzt"}});
        const double shortHeight=row->height();
        row->setProperty("title","Ein sehr langer Projektname, der auf einem Telefon bestimmt nicht in eine Zeile passt");
        row->setProperty("detail","Swift · iOS · Build nur über einen macOS-Host mit Apple-SDK und eigenem Zertifikat");
        QCoreApplication::processEvents();QTest::qWait(30);
        QVERIFY2(row->height()>shortHeight+20,qPrintable(QString::number(row->height())));
        auto *title=part("rowTitle");auto *detail=part("rowDetail");auto *meta=part("rowMeta");
        QVERIFY(title->property("lineCount").toInt()>=2);QVERIFY(detail->property("lineCount").toInt()>=2);
        QVERIFY(!title->property("truncated").toBool());QVERIFY(!detail->property("truncated").toBool());
        QVERIFY(meta->width()<=row->width()*0.4+0.5);
        for(auto *p:{title,detail,meta}){const auto b=p->mapRectToScene(p->boundingRect());QVERIFY2(b.left()>=0 && b.right()<=row->width()+0.5,qPrintable(p->objectName()));}
    }
    void long_meta_is_capped_at_forty_percent(){
        make({{"title","Flutter-Demo"},{"detail","Flutter · Android, iOS"},{"meta","vor einer sehr langen Zeitangabe, die nicht ganz passt"}});
        auto *meta=part("rowMeta");auto *title=part("rowTitle");
        QVERIFY(meta->width()<=row->width()*0.4+0.5);QVERIFY(meta->property("truncated").toBool());
        // The title keeps a readable column even beside the widest meta text allowed.
        QVERIFY2(title->width()>=row->width()*0.3,qPrintable(QString::number(title->width())));
    }
    void empty_detail_meta_and_dot_leave_no_gap(){
        make({{"title","Anderen Ordner öffnen"},{"showDot",false}});
        QVERIFY(!part("rowDetail")->isVisible());QVERIFY(!part("rowMeta")->isVisible());QVERIFY(!part("rowDot")->isVisible());
        auto *title=part("rowTitle");QCOMPARE(int(title->mapToScene(QPointF(0,0)).x()),16);
        QVERIFY(row->height()>=56 && row->height()<=72);
    }
    void mono_detail_breaks_long_paths(){
        make({{"title","android/app/src/main/AndroidManifest.xml"},{"detail","Z. 3 · package=com.example.flutterdemo.verylongpackagename.segment"},{"monoDetail",true},{"meta","1"}},360);
        auto *detail=part("rowDetail");QCOMPARE(detail->property("font").value<QFont>().pixelSize(),13);
        QVERIFY(!detail->property("truncated").toBool());
        const auto b=detail->mapRectToScene(detail->boundingRect());QVERIFY(b.right()<=row->width()+0.5);
    }
    void scales_with_the_interface_size_and_taps_reach_the_row(){
        make({{"title","Tests"},{"detail","Abgeschlossen · Bericht vorhanden"},{"meta","vor 18 min"}},412,200);
        QVERIFY2(row->height()>=112,qPrintable(QString::number(row->height())));
        QCOMPARE(part("rowTitle")->property("font").value<QFont>().pixelSize(),32);
        QSignalSpy clicked(row,SIGNAL(clicked()));
        const auto point=row->mapToScene(QPointF(row->width()/2,row->height()/2)).toPoint();
        QTest::mouseClick(window.get(),Qt::LeftButton,Qt::NoModifier,point);
        QTRY_COMPARE(clicked.count(),1);
    }
};

int main(int argc,char **argv){
    qputenv("QT_QPA_PLATFORM","offscreen");qputenv("QT_QUICK_BACKEND","software");qputenv("QT_QUICK_CONTROLS_STYLE","Basic");
    QGuiApplication app(argc,argv);MobileRowTest test;return QTest::qExec(&test,argc,argv);
}
#include "mobile_row_test.moc"
