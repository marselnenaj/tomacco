#include "nativeview.h"
#include <QGuiApplication>
#include <QQuickWindow>
#include <QLocalServer>
#include <QTemporaryDir>
#include <QFile>
#include <QBuffer>
#include <QJsonDocument>
#include <QtEndian>
#include <QtTest>

class NativeViewTest : public QObject {
    Q_OBJECT
private slots:
    void complete_h264_picture_does_not_wait_for_another_frame() {
        QTemporaryDir directory;QLocalServer video;QVERIFY(video.listen(directory.filePath("video")));
        QFile fixture(QStringLiteral(TOMACCO_SOURCE_DIR "/tests/fixtures/native-video-single.h264"));QVERIFY(fixture.open(QIODevice::ReadOnly));
        auto bytes=fixture.readAll();QByteArray packet(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;
        connect(&video,&QLocalServer::newConnection,this,[&]{auto *socket=video.nextPendingConnection();socket->write(packet);connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        QQuickWindow window;window.resize(200,400);NativeView display(window.contentItem());display.setSize({200,400});
        display.setSession({{"session",QString(32,'b')},{"codec","h264"},{"packetization","access-unit"},{"video_socket",video.fullServerName()}});
        window.show();QVERIFY(QTest::qWaitForWindowExposed(&window));
        QTRY_COMPARE_WITH_TIMEOUT(display.frameCount(),quint64(1),1000);
        QCOMPARE(display.sourceSize(),QSize(80,160));QVERIFY(!window.grabWindow().isNull());
        QTest::qWait(100);QCOMPARE(display.frameCount(),quint64(1));
    }
    void decodes_frames_and_maps_real_pointer_events_data() {
        QTest::addColumn<QString>("fixtureName");QTest::addColumn<QSize>("source");QTest::addColumn<QString>("codec");
        QTest::newRow("portrait")<<QString("native-video.h264")<<QSize(80,160)<<QString("h264");
        QTest::newRow("1080-width-simd-padding")<<QString("native-video-wide.h264")<<QSize(1080,240)<<QString("h264");
        QTest::newRow("iphone-mjpeg")<<QString()<<QSize(390,844)<<QString("mjpeg");
    }
    void decodes_frames_and_maps_real_pointer_events() {
        QFETCH(QString,fixtureName);QFETCH(QSize,source);QFETCH(QString,codec);
        QTemporaryDir directory;QVERIFY(directory.isValid());
        QLocalServer video,control;
        QVERIFY(video.listen(directory.filePath("video")));
        QVERIFY(control.listen(directory.filePath("control")));
        QByteArray bytes;
        if(codec=="mjpeg") {QImage frame(source,QImage::Format_RGB32);frame.fill(QColor("#294c73"));QBuffer output(&bytes);QVERIFY(output.open(QIODevice::WriteOnly));QVERIFY(frame.save(&output,"JPEG"));}
        else {QFile fixture(QStringLiteral(TOMACCO_SOURCE_DIR "/tests/fixtures/")+fixtureName);QVERIFY(fixture.open(QIODevice::ReadOnly));bytes=fixture.readAll();}
        QByteArray packet(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;
        QVector<QJsonObject> inputs;
        connect(&control,&QLocalServer::newConnection,this,[&]{
            auto *socket=control.nextPendingConnection();
            connect(socket,&QLocalSocket::readyRead,socket,[&,socket]{
                if(!socket->canReadLine())return;
                auto value=QJsonDocument::fromJson(socket->readLine()).object();inputs.push_back(value["arguments"].toObject());
                socket->write("{\"ok\":true,\"result\":{\"delivered\":true}}\n");socket->flush();
            });
            connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);
        });
        connect(&video,&QLocalServer::newConnection,this,[&]{
            auto *socket=video.nextPendingConnection();
            socket->write(packet.first(3));
            QTimer::singleShot(10,socket,[socket,packet]{socket->write(packet.mid(3));});
            // Several texture replacements catch lifetime errors in the rendering thread.
            auto *timer=new QTimer(socket);timer->setInterval(90);
            connect(timer,&QTimer::timeout,socket,[socket,packet]{socket->write(packet);});timer->start();
            connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);
        });
        QQuickWindow window;window.resize(480,400);window.setColor(Qt::black);
        NativeView display(window.contentItem());display.setWidth(480);display.setHeight(400);
        display.setSession({{"session",QString(32,'a')},{"codec",codec},{"video_socket",video.fullServerName()},{"control_socket",control.fullServerName()}});
        window.show();QVERIFY(QTest::qWaitForWindowExposed(&window));
        QTRY_VERIFY_WITH_TIMEOUT(display.frameCount()>3,5000);
        QCOMPARE(display.sourceSize(),source);QCOMPARE(display.state(),QString("live"));
        QVERIFY(qAbs(display.contentRect().width()/display.contentRect().height()-double(source.width())/source.height())<.0001);
        QVERIFY(!window.grabWindow().isNull());
        QTest::mouseClick(&window,Qt::LeftButton,Qt::NoModifier,QPoint(240,200));
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),2,5000);
        QCOMPARE(inputs[0]["phase"].toString(),QString("down"));QCOMPARE(inputs[1]["phase"].toString(),QString("up"));
        QCOMPARE(inputs[0]["x"].toDouble(),.5);QCOMPARE(inputs[0]["y"].toDouble(),.5);
        QCOMPARE(inputs[0]["width"].toInt(),source.width());QCOMPARE(inputs[0]["height"].toInt(),source.height());
        QTest::keyClick(&window,Qt::Key_Backspace);QTRY_COMPARE(inputs.size(),3);QCOMPARE(inputs[2]["key"].toString(),QString("backspace"));
        display.insertText(QString::fromUtf8("Grüße 🎵"));QTRY_COMPARE(inputs.size(),4);QCOMPARE(inputs[3]["text"].toString(),QString::fromUtf8("Grüße 🎵"));
        // Letterboxing is never forwarded as an app touch.
        QTest::mouseClick(&window,Qt::LeftButton,Qt::NoModifier,QPoint(1,1));QTest::qWait(50);QCOMPARE(inputs.size(),4);
        // A metadata refresh must not tear down a healthy decoder or clear its
        // last picture while the user is interacting with the same device.
        QSignalSpy sizeChanges(&display,&NativeView::sourceSizeChanged);
        QSignalSpy stateChanges(&display,&NativeView::stateChanged);
        const auto framesBeforeMetadata=display.frameCount();
        auto metadata=display.session();metadata["audio"]=QVariantMap{{"enabled",true},{"status","running"}};
        display.setSession(metadata);
        QCOMPARE(display.frameCount(),framesBeforeMetadata);QCOMPARE(display.state(),QString("live"));
        QCOMPARE(sizeChanges.size(),0);QCOMPARE(stateChanges.size(),0);
        // High-resolution touchpad events form one short gesture, rather than
        // queueing a 160 ms swipe per event. Exercise both scroll axes.
        auto wheel=[&](QPoint pixels,QPoint angles,Qt::ScrollPhase phase){
            QWheelEvent event(QPointF(240,200),QPointF(window.mapToGlobal(QPoint(240,200))),pixels,angles,Qt::NoButton,Qt::NoModifier,phase,false);
            QCoreApplication::sendEvent(&window,&event);
        };
        for(int i=0;i<32;++i)wheel(QPoint(1,-1),{},Qt::ScrollUpdate);
        wheel({},{},Qt::ScrollEnd);
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),7,1000);
        QCOMPARE(inputs[4]["phase"].toString(),QString("down"));
        QCOMPARE(inputs[5]["phase"].toString(),QString("move"));
        QCOMPARE(inputs[6]["phase"].toString(),QString("up"));
        QVERIFY(inputs[5]["x"].toDouble()>.5);QVERIFY(inputs[5]["y"].toDouble()<.5);
        for(int i=4;i<7;++i)QCOMPARE(inputs[i]["action"].toString(),QString("touch"));
        // A mouse without scroll phases still releases its virtual finger.
        wheel({},QPoint(0,120),Qt::NoScrollPhase);
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),10,1000);
        QCOMPARE(inputs.last()["phase"].toString(),QString("up"));
        QVERIFY(inputs[8]["y"].toDouble()>inputs[7]["y"].toDouble());
        // Crossing the display edge continues moving instead of generating a
        // stationary tap at the new anchor when the wheel burst ends.
        wheel({},QPoint(0,-12000),Qt::ScrollUpdate);wheel({},{},Qt::ScrollEnd);
        QTRY_COMPARE_WITH_TIMEOUT(inputs.size(),16,1000);
        for(int start:{10,13}) {
            QCOMPARE(inputs[start]["phase"].toString(),QString("down"));
            QCOMPARE(inputs[start+1]["phase"].toString(),QString("move"));
            QCOMPARE(inputs[start+2]["phase"].toString(),QString("up"));
            QVERIFY(inputs[start+1]["y"].toDouble()<inputs[start]["y"].toDouble());
        }
        auto previous=display.frameCount();QTRY_VERIFY_WITH_TIMEOUT(display.frameCount()>previous+2,2000);
        display.setSession({});QTRY_VERIFY(display.sourceSize().isEmpty());
        QCOMPARE(display.state(),QString("disconnected"));
    }
};
int main(int argc,char **argv) {
    QGuiApplication app(argc,argv);NativeViewTest test;return QTest::qExec(&test,argc,argv);
}
#include "nativeview_test.moc"
