#include "demorenderer.h"
#include <QFile>
#include <QJsonArray>
#include <QJsonObject>
#include <QTemporaryDir>
#include <QProcess>
#include <QStandardPaths>
#include <QJsonDocument>
#include <QCryptographicHash>
#include <QScopeGuard>
#include <QtTest>
#include <cmath>

class DemoRendererTest:public QObject {
    Q_OBJECT
    QTemporaryDir directory;
    QString source;
    QJsonObject document() const {
        return {{"format","tomacco.demo"},{"version",1},{"revision",1},{"title","Offline fixture"},
            {"source",QJsonObject{{"path",source},{"width",200},{"height",400},{"duration",2.0},{"rotation",0}}},
            {"clip",QJsonObject{{"start",0.0}}},{"output",QJsonObject{{"width",640},{"height",960},{"fps",30},{"duration",2.0}}},
            {"device",QJsonObject{{"model","phone"},{"finish","#242421"},{"bezel",.035},{"thickness",.065},{"corner_radius",.09}}},
            {"background",QJsonObject{{"style","solid"},{"color","#f4f2ec"},{"color2","#f4f2ec"},{"image",""}}},
            {"shadow",QJsonObject{{"enabled",false},{"opacity",.22},{"blur",24.0},{"offset_x",0.0},{"offset_y",40.0}}},
            {"camera",QJsonObject{{"distance",4.0},{"fov",35.0}}},
            {"keyframes",QJsonArray{QJsonObject{{"time",0.0},{"yaw",0.0},{"pitch",0.0},{"roll",0.0},{"scale",.72},{"x",.5},{"y",.5},{"easing","linear"}},QJsonObject{{"time",2.0},{"yaw",40.0},{"pitch",8.0},{"roll",4.0},{"scale",.8},{"x",.6},{"y",.4},{"easing","smooth"}}}}};
    }
    Demo::Scene scene() const {QString error;auto parsed=Demo::Scene::fromJson(document(),&error);Q_ASSERT(parsed);return *parsed;}
    static int countColor(const QImage &image,QColor color,int tolerance=10) {
        int count=0;for(int y=0;y<image.height();++y)for(int x=0;x<image.width();++x){const auto c=image.pixelColor(x,y);if(std::abs(c.red()-color.red())<tolerance && std::abs(c.green()-color.green())<tolerance && std::abs(c.blue()-color.blue())<tolerance)++count;}return count;
    }
    QString rendererPath() const {return QCoreApplication::applicationDirPath()+"/tomacco-demo-render";}
    QString makeVideo() const {
        const QString video=directory.filePath("source.mp4");
        if(QFileInfo::exists(video))return video;
        QProcess process;process.start(QStandardPaths::findExecutable("ffmpeg"),{"-v","error","-nostdin","-f","lavfi","-i","testsrc2=size=200x400:rate=12:duration=2","-c:v","libx264","-threads","2","-pix_fmt","yuv420p",video});
        if(!process.waitForFinished(15000) || process.exitCode()!=0)return {};return video;
    }
    QString writeScene(QJsonObject d,const QString &name) const {
        const QString filename=directory.filePath(name);QFile file(filename);if(!file.open(QIODevice::WriteOnly))return {};file.write(QJsonDocument(d).toJson());return filename;
    }
    static QByteArray read(const QString &path) {QFile f(path);if(!f.open(QIODevice::ReadOnly))return {};return f.readAll();}
private slots:
    void initTestCase() {QVERIFY(directory.isValid());source=directory.filePath("recording.fixture");QFile file(source);QVERIFY(file.open(QIODevice::WriteOnly));file.write("Only local validation; no media process is started by these tests.");}
    void rejects_invalid_scene_boundaries() {
        auto valid=document();QString error;QVERIFY(Demo::Scene::fromJson(valid,&error));
        for(const auto &patch:QList<QJsonObject>{QJsonObject{{"width",641}},QJsonObject{{"width",4000}},QJsonObject{{"fps",29}},QJsonObject{{"duration",61}}}) {
            auto d=valid;auto out=d["output"].toObject();for(auto i=patch.begin();i!=patch.end();++i)out[i.key()]=i.value();d["output"]=out;QVERIFY(!Demo::Scene::fromJson(d,&error));QVERIFY(!error.isEmpty());
        }
        auto d=valid;auto input=d["source"].toObject();input["width"]=16384;input["height"]=16384;d["source"]=input;QVERIFY(!Demo::Scene::fromJson(d,&error));
        d=valid;input=d["source"].toObject();input["path"]="https://example.invalid/movie.mp4";d["source"]=input;QVERIFY(!Demo::Scene::fromJson(d,&error));
        d=valid;auto keys=d["keyframes"].toArray();keys.append(keys.first());d["keyframes"]=keys;QVERIFY(!Demo::Scene::fromJson(d,&error));
        d=valid;auto device=d["device"].toObject();device["thickness"]=1;d["device"]=device;QVERIFY(!Demo::Scene::fromJson(d,&error));
    }
    void pose_interpolation_has_exact_endpoints_and_segment_easing() {
        auto s=scene();QCOMPARE(s.poseAt(-1).yaw,0.0);QCOMPARE(s.poseAt(3).yaw,40.0);QCOMPARE(s.poseAt(1).yaw,20.0);QCOMPARE(s.poseAt(1).x,.55);
        s.keyframes[0].easing="smooth";QCOMPARE(s.poseAt(.5).yaw,6.25);
        s.keyframes[0].easing="ease-in";QCOMPARE(s.poseAt(.5).yaw,2.5);
        s.keyframes[0].easing="ease-out";QCOMPARE(s.poseAt(.5).yaw,17.5);
    }
    void output_timestamps_are_rational_and_report_actual_duration() {
        auto s=scene();s.duration=.101;s.fps=30;QCOMPARE(s.frameCount(),4);QCOMPARE(s.frameTime(3),3./30);QVERIFY(s.frameTime(3)<s.duration);
        s.duration=60;s.fps=60;QCOMPARE(s.frameCount(),3600);QCOMPARE(s.frameTime(3599),3599./60);
        s.duration=1.001;s.fps=24;QCOMPARE(s.frameCount(),25);
    }
    void projects_real_thickness_and_keeps_recording_legible() {
        auto s=scene();QImage source(200,400,QImage::Format_RGB32);source.fill(QColor("#2288dd"));
        for(int y=20;y<380;++y)for(int x=20;x<180;++x)if((x/20+y/20)%2)source.setPixelColor(x,y,QColor("#ffee44"));
        s.keyframes.resize(1);s.keyframes[0].yaw=52;s.keyframes[0].pitch=12;
        s.thickness=.01;QString error;const auto thin=Demo::Renderer(s).render(source,0,&error);QVERIFY2(!thin.isNull(),qPrintable(error));
        s.thickness=.2;const auto thick=Demo::Renderer(s).render(source,0,&error);QVERIFY(!thick.isNull());QCOMPARE(thick.size(),QSize(640,960));QVERIFY(thin!=thick);
        int changed=0;for(int y=0;y<thin.height();++y)for(int x=0;x<thin.width();++x)if(thin.pixel(x,y)!=thick.pixel(x,y))++changed;
        QVERIFY(changed>1000);QVERIFY(countColor(thick,QColor("#2288dd"))>5000);QVERIFY(countColor(thick,QColor("#ffee44"))>5000);
        QCOMPARE(thick.pixelColor(0,0),s.background);
    }
    void mismatching_video_is_fitted_without_cropping_or_stretching() {
        auto s=scene();s.sourceWidth=400;s.sourceHeight=200;s.width=960;s.height=640;s.keyframes.resize(1);
        QImage source(400,200,QImage::Format_RGB32);const QColor colors[]={Qt::red,Qt::green,Qt::blue,Qt::magenta};
        for(int y=0;y<200;++y)for(int x=0;x<400;++x)source.setPixelColor(x,y,colors[x/100]);
        const auto image=Demo::Renderer(s).render(source,0);QVERIFY(!image.isNull());
        int minimum=10000000,maximum=0;for(auto color:colors){const int count=countColor(image,color);minimum=std::min(minimum,count);maximum=std::max(maximum,count);QVERIFY(count>10000);}QVERIFY(double(maximum)/minimum<1.03);
        // Changing the device model changes the shell/screen proportions, while
        // all four source stripes remain present inside the tablet as well.
        s.model="tablet";const auto tablet=Demo::Renderer(s).render(source,0);QVERIFY(tablet!=image);for(auto color:colors)QVERIFY(countColor(tablet,color)>5000);
    }
    void gradient_shadow_and_source_validation() {
        auto s=scene();QImage frame(200,400,QImage::Format_RGB32);frame.fill(Qt::blue);s.backgroundStyle="gradient";s.background=QColor("#000000");s.background2=QColor("#ffffff");
        s.shadow=true;const auto image=Demo::Renderer(s).render(frame,.5);QVERIFY(!image.isNull());QVERIFY(image.pixelColor(0,0).lightness()<image.pixelColor(639,959).lightness());
        auto noShadow=s;noShadow.shadow=false;QVERIFY(image!=Demo::Renderer(noShadow).render(frame,.5));
        QString error;QVERIFY(Demo::Renderer(s).render(QImage(1,1,QImage::Format_RGB32),0,&error).isNull());QVERIFY(!error.isEmpty());QVERIFY(Demo::Renderer(s).render(frame,3,&error).isNull());
    }
    void cli_exports_valid_media_and_refuses_overwrite() {
        QVERIFY(QFileInfo::exists(rendererPath()));const auto video=makeVideo();QVERIFY(!video.isEmpty());
        auto d=document();auto input=d["source"].toObject();input["path"]=video;d["source"]=input;
        auto out=d["output"].toObject();out["duration"]=.51;out["fps"]=24;out["width"]=320;out["height"]=640;d["output"]=out;
        auto keys=d["keyframes"].toArray();auto end=keys.last().toObject();end["time"]=.51;keys[1]=end;d["keyframes"]=keys;
        const auto scenePath=writeScene(d,"export.json"),output=directory.filePath("export.mp4");QVERIFY(!scenePath.isEmpty());
        QProcess renderer;renderer.start(rendererPath(),{"--scene",scenePath,"--output",output});QVERIFY(renderer.waitForFinished(30000));QCOMPARE(renderer.exitCode(),0);
        const auto metadata=QJsonDocument::fromJson(renderer.readAllStandardOutput()).object();QCOMPARE(metadata["frames"].toInt(),13);QCOMPARE(metadata["duration"].toDouble(),13./24);QVERIFY(QFileInfo::exists(output+".poster.png"));QVERIFY(QFileInfo::exists(output+".manifest.json"));
        QProcess probe;probe.start(QStandardPaths::findExecutable("ffprobe"),{"-v","error","-count_frames","-show_streams","-of","json",output});QVERIFY(probe.waitForFinished(15000));QCOMPARE(probe.exitCode(),0);
        const auto streams=QJsonDocument::fromJson(probe.readAllStandardOutput()).object()["streams"].toArray();QCOMPARE(streams.size(),1);const auto stream=streams.first().toObject();QCOMPARE(stream["codec_name"].toString(),QString("h264"));QCOMPARE(stream["nb_read_frames"].toString().toInt(),13);QCOMPARE(stream["width"].toInt(),320);QCOMPARE(stream["height"].toInt(),640);
        const auto before=QCryptographicHash::hash(read(output),QCryptographicHash::Sha256);
        renderer.start(rendererPath(),{"--scene",scenePath,"--output",output});QVERIFY(renderer.waitForFinished(10000));QVERIFY(renderer.exitCode()!=0);QCOMPARE(QCryptographicHash::hash(read(output),QCryptographicHash::Sha256),before);
        const auto blocked=directory.filePath("blocked.mp4");QFile sentinel(blocked+".poster.png");QVERIFY(sentinel.open(QIODevice::WriteOnly));sentinel.write("preserve");sentinel.close();
        renderer.start(rendererPath(),{"--scene",scenePath,"--output",blocked});QVERIFY(renderer.waitForFinished(10000));QVERIFY(renderer.exitCode()!=0);QVERIFY(!QFileInfo::exists(blocked));QVERIFY(!QFileInfo::exists(blocked+".manifest.json"));QCOMPARE(read(blocked+".poster.png"),QByteArray("preserve"));
    }
    void cli_preview_holds_end_frame_and_respects_rotation_metadata() {
        const auto video=makeVideo();QVERIFY(!video.isEmpty());const auto rotated=directory.filePath("rotated.mp4");
        QProcess rotate;rotate.start(QStandardPaths::findExecutable("ffmpeg"),{"-v","error","-nostdin","-display_rotation:v:0","90","-i",video,"-c","copy",rotated});QVERIFY(rotate.waitForFinished(15000));QCOMPARE(rotate.exitCode(),0);
        auto d=document();auto input=d["source"].toObject();input["path"]=rotated;input["width"]=400;input["height"]=200;input["rotation"]=90;d["source"]=input;
        const auto scenePath=writeScene(d,"rotate.json"),output=directory.filePath("end.png");
        QProcess renderer;renderer.start(rendererPath(),{"--scene",scenePath,"--output",output,"--time","2"});QVERIFY(renderer.waitForFinished(15000));QVERIFY2(renderer.exitCode()==0,qPrintable(QString::fromUtf8(renderer.readAllStandardError())));QCOMPARE(QImage(output).size(),QSize(640,960));
        input["width"]=200;input["height"]=400;d["source"]=input;const auto invalid=writeScene(d,"wrong-size.json"),badOutput=directory.filePath("invalid.png");
        renderer.start(rendererPath(),{"--scene",invalid,"--output",badOutput,"--time","0"});QVERIFY(renderer.waitForFinished(15000));QVERIFY(renderer.exitCode()!=0);QVERIFY(!QFileInfo::exists(badOutput));
    }
    void cli_cancel_removes_partial_artifacts_and_only_stops_own_children() {
        const auto video=makeVideo();QVERIFY(!video.isEmpty());auto d=document();auto input=d["source"].toObject();input["path"]=video;d["source"]=input;
        auto out=d["output"].toObject();out["width"]=2160;out["height"]=3840;out["fps"]=60;d["output"]=out;
        const auto scenePath=writeScene(d,"cancel.json"),output=directory.filePath("cancel.mp4");
        QProcess unrelated;unrelated.start("/usr/bin/sleep",{"30"});QVERIFY(unrelated.waitForStarted());
        auto cleanup=qScopeGuard([&]{unrelated.kill();unrelated.waitForFinished();});
        QProcess renderer;renderer.start(rendererPath(),{"--scene",scenePath,"--output",output});QVERIFY(renderer.waitForStarted());
        QByteArray log;QElapsedTimer timer;timer.start();while(!log.contains("rendering") && renderer.state()!=QProcess::NotRunning && timer.elapsed()<15000){renderer.waitForReadyRead(100);log+=renderer.readAllStandardError();}
        QVERIFY2(log.contains("rendering"),log.constData());
        const auto pid=renderer.processId();const auto children=read(QString("/proc/%1/task/%1/children").arg(pid)).simplified().split(' ');QVERIFY(!children.isEmpty());
        renderer.terminate();QVERIFY(renderer.waitForFinished(8000));QVERIFY(renderer.exitCode()!=0);QCOMPARE(unrelated.state(),QProcess::Running);
        QVERIFY(!QFileInfo::exists(output));QVERIFY(!QFileInfo::exists(output+".poster.png"));QVERIFY(!QFileInfo::exists(output+".manifest.json"));
        for(const auto &child:children)if(!child.isEmpty())QTRY_VERIFY(!QFileInfo::exists("/proc/"+QString::fromLatin1(child)));
        const auto partials=QDir(directory.path()).entryList({".tomacco-demo-*"},QDir::Files|QDir::Hidden);QVERIFY(partials.isEmpty());
    }

};
QTEST_MAIN(DemoRendererTest)
#include "demo_renderer_test.moc"
