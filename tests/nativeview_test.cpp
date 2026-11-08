#include "nativeview.h"
#include <QGuiApplication>
#include <QQuickWindow>
#include <QLocalServer>
#include <QTemporaryDir>
#include <QFile>
#include <QBuffer>
#include <QJsonDocument>
#include <QJsonArray>
#include <QProcess>
#include <QPainter>
#include <QEventLoop>
#include <QStandardPaths>
#include <QCryptographicHash>
#include <QtEndian>
#include <QtTest>
#include <algorithm>
#include <cmath>

class NativeViewTest : public QObject {
    Q_OBJECT
private slots:
    // Manual evidence only: normal native-display CI does not acquire timing
    // samples. This never connects to a runtime or physical device and never
    // shows a window. Set QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software.
    void local_video_availability_benchmark() {
        const auto output=qEnvironmentVariable("TOMACCO_NATIVEVIEW_BENCHMARK");
        if(output.isEmpty())QSKIP("Opt-in local stream benchmark; set TOMACCO_NATIVEVIEW_BENCHMARK=/path/report.json");
        QCOMPARE(qEnvironmentVariable("QT_QPA_PLATFORM"),QString("offscreen"));
        QCOMPARE(qEnvironmentVariable("QT_QUICK_BACKEND"),QString("software"));
        const auto ffmpeg=QStandardPaths::findExecutable("ffmpeg");QVERIFY2(!ffmpeg.isEmpty(),"ffmpeg is required to prepare local H264 packets");
        bool validTrials=false;int trials=qEnvironmentVariableIntValue("TOMACCO_NATIVEVIEW_BENCHMARK_TRIALS",&validTrials);
        if(!validTrials)trials=20;
        QVERIFY(trials>=3 && trials<=1000);
        const auto stats=[](QVector<double> values) {
            std::sort(values.begin(),values.end());const int n=values.size();
            return QJsonObject{{"n",n},{"p50_ms",n%2?values[n/2]:(values[n/2-1]+values[n/2])/2},
                               {"p95_ms",values[int(std::ceil(.95*n))-1]},{"min_ms",values.first()},{"max_ms",values.last()}};
        };
        QJsonObject report{{"schema","tomacco-local-nativeview/1"},
            {"evidence","synthetic local socket packets; offscreen software; no shown window"},
            {"boundary","before QLocalSocket::write/flush to NativeView::frameChanged on GUI thread, after its image assignment; monotonic event timestamps, no QTRY polling"},
            {"excluded",QJsonArray{"frame encoding and PNG correctness-save time","texture creation/upload and GPU memory","frameSwapped/display presentation and monitor latency","device capture, USB/AirPlay/network and touch","realistic content complexity or device FPS"}},
            {"limits",QJsonArray{"Synthetic patterned color frames, not representative camera/app motion","p95 nearest rank; short samples are exploratory","bursts measure emitted GUI update coalescing, not decoder frame loss","100ms quiescence is a bounded observation, not proof of no future events"}},
            {"trials",trials},{"qt",qVersion()},{"cases",QJsonArray{}}};
        QJsonArray cases;
        QTemporaryDir directory;QVERIFY(directory.isValid());
        const QVector<QColor> colors{QColor(210,40,30),QColor(30,190,60),QColor(40,60,210)};
        for(const QSize source:{QSize(1080,1920),QSize(1440,3120),QSize(3840,2160)}) {
            QVector<QByteArray> jpeg,h264;
            for(const auto color:colors) {
                QImage picture(source,QImage::Format_RGB888);picture.fill(color);
                {QPainter painter(&picture);
                    for(int y=0;y<source.height();y+=96)for(int x=0;x<source.width();x+=96)
                        if((x/96+y/96)%2)painter.fillRect(x,y,48,48,color.lighter(130));
                    painter.fillRect(source.width()/2-80,source.height()/2-80,160,160,color);
                }
                QByteArray bytes;QBuffer outputBuffer(&bytes);QVERIFY(outputBuffer.open(QIODevice::WriteOnly));QVERIFY(picture.save(&outputBuffer,"JPEG",85));jpeg.append(bytes);
                QProcess encoder;
                encoder.start(ffmpeg,{"-hide_banner","-loglevel","error","-f","rawvideo","-pixel_format","rgb24","-video_size",QString::number(source.width())+"x"+QString::number(source.height()),
                                      "-i","pipe:0","-frames:v","1","-an","-c:v","libx264","-threads","2","-preset","ultrafast","-tune","zerolatency","-crf","18","-pix_fmt","yuv420p","-f","h264","pipe:1"});
                QVERIFY(encoder.waitForStarted(10000));
                const QByteArray raw(reinterpret_cast<const char *>(picture.constBits()),picture.sizeInBytes());
                QCOMPARE(encoder.write(raw),qint64(raw.size()));encoder.closeWriteChannel();
                QVERIFY(encoder.waitForFinished(30000));
                const auto error=encoder.readAllStandardError();QVERIFY2(encoder.exitStatus()==QProcess::NormalExit && encoder.exitCode()==0,error.constData());
                h264.append(encoder.readAllStandardOutput());QVERIFY(!h264.last().isEmpty());
            }
            for(const auto codec:{QString("mjpeg"),QString("h264")}) {
                const auto &encoded=codec=="mjpeg"?jpeg:h264;
                QVector<QByteArray> packets;QJsonArray packetBytes,packetHashes;
                for(const auto &bytes:encoded) {
                    QByteArray packet(4,0);qToBigEndian(quint32(bytes.size()),packet.data());packet+=bytes;packets.append(packet);
                    packetBytes.append(packet.size());packetHashes.append(QString::fromLatin1(QCryptographicHash::hash(packet,QCryptographicHash::Sha256).toHex()));
                }
                QLocalServer video;QVERIFY(video.listen(directory.filePath(codec+QString::number(source.width()))));
                QLocalSocket *sender=nullptr;QEventLoop connected;QTimer connectTimeout;connectTimeout.setSingleShot(true);
                connect(&connectTimeout,&QTimer::timeout,&connected,&QEventLoop::quit);
                connect(&video,&QLocalServer::newConnection,&connected,[&]{sender=video.nextPendingConnection();connected.quit();});
                QQuickWindow window;window.resize(320,480);NativeView display(window.contentItem());display.setSize({320,480});
                display.setSession({{"session",QString(32,'b')},{"codec",codec},{"packetization","access-unit"},{"video_socket",video.fullServerName()}});
                connectTimeout.start(3000);if(!sender)connected.exec();QVERIFY(sender);connectTimeout.stop();
                QElapsedTimer clock;clock.start();double fedAt=0,lastEvent=0;int events=0;
                QEventLoop wait;QTimer deadline;deadline.setSingleShot(true);connect(&deadline,&QTimer::timeout,&wait,&QEventLoop::quit);
                connect(&display,&NativeView::frameChanged,&wait,[&]{
                    if(display.state()!="live" || display.sourceSize().isEmpty())return;
                    lastEvent=clock.nsecsElapsed()/1e6;++events;wait.quit();
                });
                auto feed=[&](const QByteArray &packet) {
                    const int before=events;fedAt=clock.nsecsElapsed()/1e6;
                    if(sender->write(packet)!=packet.size())return false;sender->flush();
                    deadline.start(3000);if(events==before)wait.exec();deadline.stop();
                    return events>before;
                };
                auto correct=[&](const QColor &color) {
                    const auto path=directory.filePath("decoded.png");if(!display.saveFrame(path))return false;
                    const QImage saved(path);if(saved.size()!=source)return false;
                    auto close=[](const QColor &actual,const QColor &expected) {
                        return qAbs(actual.red()-expected.red())<18 && qAbs(actual.green()-expected.green())<18 && qAbs(actual.blue()-expected.blue())<18;
                    };
                    if(!close(saved.pixelColor(source.width()/2,source.height()/2),color))return false;
                    for(int y:{8,source.height()/4+8,source.height()*3/4+8,source.height()-8})
                        for(int x:{8,source.width()/4+8,source.width()*3/4+8,source.width()-8}) {
                            auto expected=((x/96+y/96)%2 && x%96<48 && y%96<48)?color.lighter(130):color;
                            if(qAbs(x-source.width()/2)<80 && qAbs(y-source.height()/2)<80)expected=color;
                            if(!close(saved.pixelColor(x,y),expected))return false;
                        }
                    return true;
                };
                QVector<double> latencies;QJsonArray samples;
                for(int trial=0;trial<trials;++trial) {
                    const int index=trial%colors.size();QVERIFY2(feed(packets[index]),qPrintable(display.error()));
                    const double latency=lastEvent-fedAt;
                    QVERIFY(correct(colors[index]));
                    latencies.append(latency);samples.append(latency);
                }
                // The last blue packet is distinct from every earlier packet in
                // this burst, so a fresh final picture cannot pass by coincidence.
                QByteArray burst;for(int i=0;i<8;++i)burst+=packets[i%2];burst+=packets[2];
                const int beforeBurst=events;QVERIFY(feed(burst));
                QEventLoop settle;QTimer::singleShot(100,&settle,&QEventLoop::quit);settle.exec();
                const double burstLastMs=lastEvent-fedAt;QVERIFY(correct(colors[2]));
                const int burstEvents=events-beforeBurst;QVERIFY(burstEvents>=1 && burstEvents<=9);
                const int beforeQuiet=events;QTimer::singleShot(100,&settle,&QEventLoop::quit);settle.exec();QCOMPARE(events,beforeQuiet);
                QVERIFY(feed(packets[0]));const double recoveryMs=lastEvent-fedAt;QVERIFY(correct(colors[0]));
                QVector<double> warm=latencies;warm.removeFirst();
                QJsonObject result{{"width",source.width()},{"height",source.height()},{"codec",codec},
                    {"sequential",stats(latencies)},{"first_frame_ms",latencies.first()},{"subsequent_frames",stats(warm)},
                    {"samples_ms",samples},{"correct_frames",trials},{"encoded_packet_bytes",packetBytes},{"encoded_packet_sha256",packetHashes},
                    {"burst",QJsonObject{{"packets",9},{"gui_frame_events",burstEvents},{"coalesced_updates",9-burstEvents},{"last_frame_gui_available_ms",burstLastMs},{"last_color_correct",true},{"no_more_events_for_ms",100},{"recovery_ms",recoveryMs},{"recovery_color_correct",true}}}};
                cases.append(result);report["cases"]=cases;
                QFile proof(output);QVERIFY(proof.open(QIODevice::WriteOnly));QVERIFY(proof.write(QJsonDocument(report).toJson())>0);proof.close();
                display.setSession({});sender->close();delete sender;video.close();
            }
        }
        report["passed"]=true;QFile proof(output);QVERIFY(proof.open(QIODevice::WriteOnly));QVERIFY(proof.write(QJsonDocument(report).toJson())>0);
    }
    void airplay_hover_does_not_interrupt_a_pressed_or_wheel_drag_data() {
        QTest::addColumn<bool>("wheel");
        QTest::newRow("held-mouse-button")<<false;
        QTest::newRow("wheel-burst")<<true;
    }
    void airplay_hover_does_not_interrupt_a_pressed_or_wheel_drag() {
        QFETCH(bool,wheel);
        QTemporaryDir directory;QLocalServer video,control;
        QVERIFY(video.listen(directory.filePath("video")));QVERIFY(control.listen(directory.filePath("control")));
        QImage picture(200,400,QImage::Format_RGB32);picture.fill(Qt::blue);
        QByteArray jpeg;QBuffer buffer(&jpeg);QVERIFY(buffer.open(QIODevice::WriteOnly));QVERIFY(picture.save(&buffer,"JPEG"));
        QByteArray packet(4,0);qToBigEndian(quint32(jpeg.size()),packet.data());packet+=jpeg;
        QVector<QJsonObject> inputs;
        connect(&video,&QLocalServer::newConnection,this,[&]{auto *socket=video.nextPendingConnection();socket->write(packet);connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);});
        connect(&control,&QLocalServer::newConnection,this,[&]{
            auto *socket=control.nextPendingConnection();
            connect(socket,&QLocalSocket::readyRead,socket,[&,socket]{
                if(!socket->canReadLine())return;
                const auto input=QJsonDocument::fromJson(socket->readLine()).object()["arguments"].toObject();inputs.append(input);
                // An AirPlay press first rests the pointer before pressing it.
                QTimer::singleShot(input["phase"]=="down"?60:0,socket,[socket]{socket->write("{\"ok\":true}\n");});
            });
            connect(socket,&QLocalSocket::disconnected,socket,&QObject::deleteLater);
        });
        QQuickWindow window;window.resize(200,400);NativeView display(window.contentItem());display.setSize({200,400});
        display.setSession({{"session",QString(32,'c')},{"transport","wifi/airplay"},{"codec","mjpeg"},{"video_socket",video.fullServerName()},{"control_socket",control.fullServerName()}});
        window.show();QVERIFY(QTest::qWaitForWindowExposed(&window));QTRY_COMPARE(display.sourceSize(),QSize(200,400));
        QTest::mouseMove(&window,QPoint(100,300));QTRY_VERIFY(!inputs.isEmpty() && inputs.last()["phase"]=="hover");inputs.clear();
        if(wheel) {
            QWheelEvent event(QPointF(100,300),window.mapToGlobal(QPoint(100,300)),QPoint(0,-40),{},Qt::NoButton,Qt::NoModifier,Qt::ScrollUpdate,false);
            QCoreApplication::sendEvent(&window,&event);
        } else QTest::mousePress(&window,Qt::LeftButton,Qt::NoModifier,QPoint(100,300));
        for(const int y:{280,240,200})QTest::mouseMove(&window,QPoint(100,y));
        if(wheel) {
            QHoverEvent leave(QEvent::HoverLeave,QPointF(-1,-1),window.mapToGlobal(QPoint(-1,-1)),QPointF(100,200));
            QCoreApplication::sendEvent(&display,&leave);
            QWheelEvent end(QPointF(100,200),window.mapToGlobal(QPoint(100,200)),{},{},Qt::NoButton,Qt::NoModifier,Qt::ScrollEnd,false);
            QCoreApplication::sendEvent(&window,&end);
        } else QTest::mouseRelease(&window,Qt::LeftButton,Qt::NoModifier,QPoint(100,200));
        QElapsedTimer elapsed;elapsed.start();
        while(elapsed.elapsed()<3000 && std::none_of(inputs.begin(),inputs.end(),[](const QJsonObject &input){return input["phase"]=="up";}))QTest::qWait(20);
        QStringList phases;for(const auto &input:inputs){phases.append(input["phase"].toString());if(input["phase"]=="up")break;}
        QCOMPARE(phases.join(','),QString("down,move,up"));
        QVERIFY(inputs[1]["y"].toDouble()<inputs[0]["y"].toDouble());
    }
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
