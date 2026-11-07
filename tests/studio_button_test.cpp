// The button as a local QML contract: no Control, CLI or devices. It loads
// qml/StudioButton.qml into small host scenes and checks the phone rules for
// translated action labels: a touch text button never sticks out past its
// row, its label wraps instead of being cut and the button grows to fit, an
// icon sits at the start (the right in a mirrored layout) without covering
// the text, and focus, keys, taps and the accessible name keep working. The
// desktop button keeps one eliding line and the icon button its square target.
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

class ButtonStyle : public QObject {
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

class StudioButtonTest : public QObject {
    Q_OBJECT
    ButtonStyle style;
    std::unique_ptr<QQmlEngine> engine;std::unique_ptr<QQuickWindow> window;
    QQuickItem *root=nullptr;
    static QQuickItem *visualItem(QQuickItem *parent,const QString &name){
        if(parent->objectName()==name)return parent;
        for(auto *child:parent->childItems())if(auto *found=visualItem(child,name))return found;
        return nullptr;
    }
    QQuickItem *item(const QString &name){return visualItem(root,name);}
    static QQuickItem *label(QQuickItem *button){return visualItem(button,"studioButtonLabel");}
    static QQuickItem *icon(QQuickItem *button){return visualItem(button,"studioButtonIcon");}
    static QRectF scene(QQuickItem *i){return i->mapRectToScene(QRectF(0,0,i->width(),i->height()));}
    // Where the label's glyphs are, in scene coordinates.
    static QRectF ink(QQuickItem *text){
        const double w=qMin(text->property("contentWidth").toDouble(),text->width());
        const int align=text->property("effectiveHorizontalAlignment").toInt();
        const double x=align==Qt::AlignRight?text->width()-w:align==Qt::AlignHCenter?(text->width()-w)/2:0;
        return text->mapRectToScene(QRectF(x,0,w,text->property("contentHeight").toDouble()));
    }
    QVariant eval(QObject *scope,const QString &expression){
        QQmlExpression e(engine->contextForObject(scope),scope,expression);return e.evaluate();
    }
    void settle(){for(int i=0;i<4;++i){QCoreApplication::processEvents();QTest::qWait(20);}}
    void make(const QByteArray &body,int width,int scale=100,bool rtl=false){
        style.setScale(scale);
        const QByteArray qml="import QtQuick\nimport QtQuick.Controls\nimport QtQuick.Layouts\nimport \".\"\nItem {\n width: "+QByteArray::number(width)
            +"; height: 1600\n LayoutMirroring.enabled: "+(rtl?"true":"false")+"; LayoutMirroring.childrenInherit: true\n"+body+"\n}\n";
        QQmlComponent component(engine.get());
        component.setData(qml,QUrl::fromLocalFile(QStringLiteral(TOMACCO_SOURCE_DIR "/qml/StudioButtonTestHost.qml")));
        QVERIFY2(component.isReady(),qPrintable(component.errorString()));
        root=qobject_cast<QQuickItem*>(component.create());
        QVERIFY2(root,qPrintable(component.errorString()));
        window->resize(qMax(width,200),1600);root->setParentItem(window->contentItem());
        settle();
    }
    // A touch text button with its whole label readable inside its own box, within the screen.
    void readable(QQuickItem *button,double screen){
        auto *text=label(button);QVERIFY(text);QVERIFY(text->isVisible());
        QVERIFY2(!text->property("truncated").toBool(),qPrintable(text->property("text").toString()));
        const QRectF b=scene(button),k=ink(text);
        QVERIFY2(b.left()>=-0.5 && b.right()<=screen+0.5,qPrintable(QStringLiteral("button %1..%2 of %3").arg(b.left()).arg(b.right()).arg(screen)));
        QVERIFY2(k.left()>=b.left()-0.5 && k.right()<=b.right()+0.5 && k.top()>=b.top()-0.5 && k.bottom()<=b.bottom()+0.5,
                 qPrintable(QStringLiteral("label %1,%2 %3x%4 outside button %5,%6 %7x%8").arg(k.x()).arg(k.y()).arg(k.width()).arg(k.height()).arg(b.x()).arg(b.y()).arg(b.width()).arg(b.height())));
        QVERIFY(button->height()>=button->property("touchSize").toDouble()-0.5);
        if(auto *glyph=icon(button);glyph && glyph->isVisible()){
            const QRectF g=scene(glyph);
            QVERIFY2(g.right()<=k.left()+0.5 || g.left()>=k.right()-0.5,qPrintable(QStringLiteral("icon %1..%2 covers label %3..%4").arg(g.left()).arg(g.right()).arg(k.left()).arg(k.right())));
        }
    }
private slots:
    void init(){
        style.scale=100;style.dark=true;
        engine=std::make_unique<QQmlEngine>();engine->rootContext()->setContextProperty("Studio",&style);
        window=std::make_unique<QQuickWindow>();window->setColor(QColor("#0e0e0c"));window->resize(393,1600);window->show();
        QVERIFY(QTest::qWaitForWindowExposed(window.get()));
    }
    void cleanup(){delete root;root=nullptr;window.reset();engine.reset();}

    void long_touch_label_in_a_flow_wraps_inside_the_row(){
        // A 360 px phone at 200 %: 296 px between the gutters.
        make(R"(Flow { objectName: "row"; x: 32; width: 296; spacing: 8
                StudioButton { objectName: "long"; touch: true; quiet: true; iconName: "copy"; text: "Weitere Build-Optionen für dieses Projekt anzeigen" }
                StudioButton { objectName: "short"; touch: true; quiet: true; text: "Tests" } })",360,200);
        auto *row=item("row");auto *longButton=item("long");auto *shortButton=item("short");
        QVERIFY(row);QVERIFY(longButton);QVERIFY(shortButton);
        const double touch=longButton->property("touchSize").toDouble();
        QVERIFY2(touch>=96,qPrintable(QString::number(touch)));
        QVERIFY2(longButton->width()<=row->width()+0.5,qPrintable(QString::number(longButton->width())));
        QVERIFY(longButton->property("naturalWidth").toDouble()>row->width());
        QVERIFY2(label(longButton)->property("lineCount").toInt()>=2,qPrintable(QString::number(label(longButton)->property("lineCount").toInt())));
        QVERIFY2(longButton->height()>touch,qPrintable(QString::number(longButton->height())));
        readable(longButton,360);
        // A short label stays one line in an ordinary 48 px (here 96 px) target.
        QCOMPARE(label(shortButton)->property("lineCount").toInt(),1);QCOMPARE(shortButton->height(),touch);
        readable(shortButton,360);
        // The row moves the next button below the grown one, not over it.
        QVERIFY(scene(shortButton).top()>=scene(longButton).bottom()-0.5 || scene(shortButton).left()>=scene(longButton).right()-0.5);
    }
    void icon_sits_at_the_start_and_never_covers_the_label(){
        for(const bool rtl:{false,true}){
            make(R"(Flow { objectName: "row"; x: 16; width: 200; spacing: 8
                    StudioButton { objectName: "b"; touch: true; iconName: "refresh"; text: "إعادة تحميل الملف من القرص الآن" } })",232,100,rtl);
            auto *button=item("b");QVERIFY(button);
            QCOMPARE(button->property("mirrored").toBool(),rtl);
            QVERIFY(label(button)->property("lineCount").toInt()>=2);
            readable(button,232);
            const QRectF g=scene(icon(button)),b=scene(button),k=ink(label(button));
            if(rtl){QVERIFY2(g.right()>b.center().x() && g.left()>=k.right()-0.5,"the icon belongs at the right, before the text");
                QCOMPARE(label(button)->property("effectiveHorizontalAlignment").toInt(),int(Qt::AlignRight));}
            else{QVERIFY2(g.left()<b.center().x() && g.right()<=k.left()+0.5,"the icon belongs at the left, before the text");}
            delete root;root=nullptr;
        }
    }
    void full_width_layout_button_wraps_and_pushes_the_page_down(){
        make(R"(ColumnLayout { width: parent.width; spacing: 0
                StudioButton { objectName: "more"; Layout.fillWidth: true; Layout.leftMargin: 8; Layout.rightMargin: 32; touch: true; quiet: true; alignLeft: true; iconName: "chevronRight"; text: "Weitere Build-Optionen und erweiterte Einstellungen" }
                Rectangle { objectName: "below"; Layout.fillWidth: true; implicitHeight: 20 } })",360,200);
        auto *button=item("more");auto *below=item("below");QVERIFY(button);QVERIFY(below);
        QCOMPARE(button->width(),320.0);
        QVERIFY(label(button)->property("lineCount").toInt()>=2);
        readable(button,360);
        QVERIFY2(scene(below).top()>=scene(button).bottom()-0.5,"the next item sits below the grown button");
    }
    void shrinkable_layout_button_keeps_its_width_until_the_screen_is_narrow(){
        // The pattern for a button that keeps its own width in a column (Mac hosts, Find devices).
        make(R"(ColumnLayout { objectName: "column"; width: parent.width; spacing: 0
                StudioButton { objectName: "b"; Layout.fillWidth: true; Layout.maximumWidth: naturalWidth; touch: true; quiet: true; text: "Mac-Hosts verwalten und einrichten" } })",800,100);
        auto *button=item("b");auto *column=item("column");QVERIFY(button);QVERIFY(column);
        QCOMPARE(button->width(),button->property("naturalWidth").toDouble());
        QCOMPARE(label(button)->property("lineCount").toInt(),1);QCOMPARE(button->height(),button->property("touchSize").toDouble());
        // Then a narrow phone at the largest size, changed while running: the column is not pushed wider.
        style.setScale(200);root->setWidth(180);settle();
        QVERIFY2(column->width()<=180.5,qPrintable(QString::number(column->width())));
        QVERIFY2(button->width()<=180.5,qPrintable(QString::number(button->width())));
        QVERIFY(label(button)->property("lineCount").toInt()>=2);
        readable(button,180);
        // And back: one line at its own width again.
        style.setScale(100);root->setWidth(800);settle();
        QCOMPARE(button->width(),button->property("naturalWidth").toDouble());QCOMPARE(label(button)->property("lineCount").toInt(),1);
    }
    void desktop_button_keeps_one_eliding_line(){
        make(R"(Row { StudioButton { objectName: "d"; width: 120; iconName: "copy"; text: "Eine sehr lange Beschriftung für den Desktop" }
                StudioButton { objectName: "n"; text: "Kopieren" } })",400,100);
        auto *button=item("d");auto *natural=item("n");QVERIFY(button);QVERIFY(natural);
        QVERIFY(!button->property("wraps").toBool());
        QCOMPARE(button->height(),button->property("touchSize").toDouble());
        QCOMPARE(label(button)->property("lineCount").toInt(),1);QVERIFY(label(button)->property("truncated").toBool());
        // Unconstrained, a desktop button is exactly as wide as its label and padding, as before.
        QCOMPARE(natural->width(),natural->property("naturalWidth").toDouble());
        QCOMPARE(natural->height(),natural->property("touchSize").toDouble());
    }
    void icon_only_touch_button_stays_a_square_named_target(){
        make(R"(Flow { width: 120
                StudioButton { objectName: "i"; touch: true; quiet: true; iconName: "back"; text: "Zurück zur Projektliste mit einem langen Namen"; displayText: ""; tip: text } })",120,200);
        auto *button=item("i");QVERIFY(button);
        const double touch=button->property("touchSize").toDouble();QVERIFY(touch>=96);
        QCOMPARE(button->width(),touch);QCOMPARE(button->height(),touch);
        QVERIFY(!label(button)->isVisible());
        QCOMPARE(eval(button,"Accessible.name").toString(),QStringLiteral("Zurück zur Projektliste mit einem langen Namen"));
        QCOMPARE(eval(button,"ToolTip.text").toString(),QStringLiteral("Zurück zur Projektliste mit einem langen Namen"));
    }
    void wrapped_button_keeps_focus_keys_taps_and_its_name(){
        make(R"(Flow { x: 16; width: 200
                StudioButton { objectName: "b"; touch: true; text: "Gerät koppeln und über WLAN verbinden"; tip: "Kopplungscode eingeben" } })",232,200);
        auto *button=item("b");QVERIFY(button);
        QVERIFY(label(button)->property("lineCount").toInt()>=2);
        QCOMPARE(eval(button,"Accessible.name").toString(),QStringLiteral("Gerät koppeln und über WLAN verbinden"));
        QCOMPARE(eval(button,"Accessible.description").toString(),QStringLiteral("Kopplungscode eingeben"));
        QSignalSpy clicked(button,SIGNAL(clicked()));
        window->requestActivate();button->forceActiveFocus(Qt::TabFocusReason);
        QTRY_VERIFY(button->hasActiveFocus());
        QTest::keyClick(window.get(),Qt::Key_Space);
        QTRY_COMPARE(clicked.count(),1);
        // A tap on the second line (the part of the button that grew) still reaches it.
        const QPoint low=button->mapToScene(QPointF(button->width()/2,button->height()-6)).toPoint();
        QTest::mouseClick(window.get(),Qt::LeftButton,Qt::NoModifier,low);
        QTRY_COMPARE(clicked.count(),2);
    }
};

int main(int argc,char **argv){
    qputenv("QT_QPA_PLATFORM","offscreen");qputenv("QT_QUICK_BACKEND","software");qputenv("QT_QUICK_CONTROLS_STYLE","Basic");
    QGuiApplication app(argc,argv);StudioButtonTest test;return QTest::qExec(&test,argc,argv);
}
#include "studio_button_test.moc"
