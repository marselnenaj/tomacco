#include "demorenderer.h"

#include <QCommandLineParser>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcess>
#include <QStandardPaths>
#include <QTemporaryFile>
#include <QElapsedTimer>
#include <csignal>
#include <cstdio>
#include <cmath>
#include <algorithm>
#include <memory>
#include <sys/stat.h>
#include <unistd.h>

namespace {
volatile std::sig_atomic_t interrupted=0;
void cancel(int) {interrupted=1;}
void progress(const QJsonObject &value) {const auto bytes=QJsonDocument(value).toJson(QJsonDocument::Compact);std::fwrite(bytes.constData(),1,size_t(bytes.size()),stderr);std::fputc('\n',stderr);std::fflush(stderr);}
struct Child {
    QProcess process;
    ~Child(){if(process.state()!=QProcess::NotRunning){process.terminate();if(!process.waitForFinished(1000)){process.kill();process.waitForFinished(1000);}}}
    bool start(const QString &exe,const QStringList &args,QString &error) {
        process.setProgram(exe);process.setArguments(args);process.start();
        if(!process.waitForStarted(5000)){error="Could not start "+QFileInfo(exe).fileName()+": "+process.errorString();return false;}return true;
    }
    QString diagnostics(){return QString::fromUtf8(process.readAllStandardError()).right(4096).trimmed();}
};
bool waitFinished(Child &child,int timeout,QString &error) {
    QElapsedTimer timer;timer.start();
    while(child.process.state()!=QProcess::NotRunning) {
        if(interrupted){error="Rendering canceled.";return false;}
        if(timer.elapsed()>timeout){error="The local media process timed out.";return false;}
        child.process.waitForFinished(100);
    }
    if(child.process.exitStatus()!=QProcess::NormalExit || child.process.exitCode()!=0){error="The local media process failed: "+child.diagnostics();return false;}return true;
}
bool readFrame(Child &decoder,QImage &frame,QString &error) {
    const qsizetype needed=frame.sizeInBytes();qsizetype offset=0;QElapsedTimer timer;timer.start();
    while(offset<needed) {
        if(interrupted){error="Rendering canceled.";return false;}
        const auto n=decoder.process.read(reinterpret_cast<char*>(frame.bits())+offset,needed-offset);
        if(n>0){offset+=n;timer.restart();continue;}
        if(decoder.process.state()==QProcess::NotRunning){error="The video ended before a complete requested frame: "+decoder.diagnostics();return false;}
        if(timer.elapsed()>30000){error="Timed out decoding a local video frame.";return false;}
        decoder.process.waitForReadyRead(100);
    }
    return true;
}
bool writeFrame(Child &encoder,const QImage &frame,QString &error) {
    const char *bytes=reinterpret_cast<const char*>(frame.constBits());qsizetype offset=0;const auto total=frame.sizeInBytes();QElapsedTimer timer;timer.start();
    while(offset<total || encoder.process.bytesToWrite()>0) {
        if(interrupted){error="Rendering canceled.";return false;}
        if(encoder.process.state()==QProcess::NotRunning){error="The video encoder stopped: "+encoder.diagnostics();return false;}
        if(encoder.process.bytesToWrite()<1024*1024 && offset<total) {
            const auto n=encoder.process.write(bytes+offset,std::min<qsizetype>(1024*1024,total-offset));
            if(n<0){error="Could not send the rendered frame to the video encoder.";return false;}offset+=n;
        }
        if(encoder.process.waitForBytesWritten(100))timer.restart();
        if(timer.elapsed()>30000){error="Timed out writing a rendered video frame.";return false;}
    }
    return true;
}
class Artifact {
public:
    explicit Artifact(QString path):destination(std::move(path)),temporary(QFileInfo(destination).absolutePath()+"/.tomacco-demo-XXXXXX"){}
    ~Artifact(){if(published && !retain){struct stat ours{},now{};if(::stat(QFile::encodeName(temporary.fileName()).constData(),&ours)==0 && ::lstat(QFile::encodeName(destination).constData(),&now)==0 && ours.st_dev==now.st_dev && ours.st_ino==now.st_ino)::unlink(QFile::encodeName(destination).constData());}}
    bool prepare(QString &error) {
        if(!QFileInfo(destination).isAbsolute() || !QFileInfo(QFileInfo(destination).absolutePath()).isDir()){error="The output must be an absolute path in an existing local directory.";return false;}
        struct stat existing{};if(::lstat(QFile::encodeName(destination).constData(),&existing)==0){error="Refusing to overwrite an existing output: "+destination;return false;}
        if(!temporary.open()){error="Cannot create an output temporary file: "+temporary.errorString();return false;}temporary.close();return true;
    }
    bool publish(QString &error) {
        if(interrupted){error="Rendering canceled.";return false;}
        // link(2) publishes without replacing an existing file, including a
        // destination created concurrently after validation. Both are in one directory.
        if(::link(QFile::encodeName(temporary.fileName()).constData(),QFile::encodeName(destination).constData())!=0){error="Could not publish the complete artifact without overwriting: "+destination;return false;}
        published=true;return true;
    }
    bool writeJson(const QJsonObject &object,QString &error) {
        QFile file(temporary.fileName());if(!file.open(QIODevice::WriteOnly|QIODevice::Truncate)){error=file.errorString();return false;}
        const auto bytes=QJsonDocument(object).toJson(QJsonDocument::Indented);
        if(file.write(bytes)!=bytes.size() || !file.flush()){error=file.errorString();return false;}return true;
    }
    QString destination;QTemporaryFile temporary;bool published=false,retain=false;
};
bool verifyDisplaySize(const Demo::Scene &scene,const QString &ffprobe,QString &error) {
    Child probe;
    if(!probe.start(ffprobe,{"-v","error","-protocol_whitelist","file,pipe","-select_streams","v:0","-show_entries","stream=width,height:stream_tags=rotate:stream_side_data=rotation","-of","json",scene.sourcePath},error) || !waitFinished(probe,15000,error))return false;
    const auto bytes=probe.process.readAllStandardOutput();if(bytes.size()>256*1024){error="Unexpectedly large video metadata.";return false;}
    const auto streams=QJsonDocument::fromJson(bytes).object().value("streams").toArray();if(streams.isEmpty()){error="The source has no readable video stream.";return false;}
    const auto stream=streams.first().toObject();int w=stream.value("width").toInt(),h=stream.value("height").toInt();int rotation=stream.value("tags").toObject().value("rotate").toString().toInt();
    for(auto entry:stream.value("side_data_list").toArray())if(entry.toObject().contains("rotation"))rotation=qRound(entry.toObject().value("rotation").toDouble());
    rotation=((rotation%360)+360)%360;if(rotation==90 || rotation==270)std::swap(w,h);
    if(w!=scene.sourceWidth || h!=scene.sourceHeight){error="The video's display dimensions changed; inspect it again before rendering.";return false;}return true;
}
QStringList decoderArguments(const Demo::Scene &scene,std::optional<double> time) {
    QStringList args{"-v","error","-nostdin","-threads","2","-protocol_whitelist","file,pipe","-ss",QString::number(scene.clipStart,'f',9),"-i",scene.sourcePath,"-map","0:v:0","-an","-sn","-dn"};
    QString filter=QString("setpts=PTS-STARTPTS,fps=fps=%1:start_time=0:round=near:eof_action=pass").arg(scene.fps);
    if(time) {
        // Sampling through the same rational timeline as export makes a paused
        // preview match that output frame, including the held final source frame.
        const int index=std::clamp(int(std::floor(*time*scene.fps)),0,scene.frameCount()-1);
        filter+=QString(",select=eq(n\\,%1)").arg(index);
    }
    args<<"-vf"<<filter;
    args<<"-frames:v"<<QString::number(time?1:scene.frameCount())<<"-threads"<<"2"<<"-pix_fmt"<<"rgba"<<"-f"<<"rawvideo"<<"pipe:1";return args;
}
int run(const QString &scenePath,const QString &output,std::optional<double> time,QString &error) {
    QFile input(scenePath);if(!QFileInfo(scenePath).isAbsolute() || !QFileInfo(scenePath).isFile() || !input.open(QIODevice::ReadOnly) || input.size()>1024*1024){error="Could not read a demo scene document of at most 1 MiB.";return 1;}
    QJsonParseError parseError;const auto document=QJsonDocument::fromJson(input.readAll(),&parseError);
    if(parseError.error!=QJsonParseError::NoError || !document.isObject()){error="The demo scene document is not valid JSON.";return 1;}
    const auto parsed=Demo::Scene::fromJson(document.object(),&error);if(!parsed)return 1;const auto &scene=*parsed;
    if(time && (!std::isfinite(*time) || *time<0 || *time>scene.duration)){error="Preview time is outside the scene duration.";return 1;}
    const QString ffmpeg=QStandardPaths::findExecutable("ffmpeg"),ffprobe=QStandardPaths::findExecutable("ffprobe");
    if(ffmpeg.isEmpty() || ffprobe.isEmpty()){error="Local ffmpeg and ffprobe executables are required.";return 1;}
    Artifact movie(output),poster(output+".poster.png"),manifest(output+".manifest.json");
    if(!movie.prepare(error) || (!time && (!poster.prepare(error) || !manifest.prepare(error))))return 1;
    if(!verifyDisplaySize(scene,ffprobe,error))return 1;
    Child decoder,encoder;if(!decoder.start(ffmpeg,decoderArguments(scene,time),error))return 1;
    if(!time) {
        QStringList args{"-v","error","-nostdin","-threads","2"};
        if(scene.sourceAudio)args<<"-copyts"<<"-start_at_zero";
        args<<"-f"<<"rawvideo"<<"-pixel_format"<<"rgba"<<"-video_size"<<QString("%1x%2").arg(scene.width).arg(scene.height)<<"-framerate"<<QString::number(scene.fps)<<"-i"<<"pipe:0";
        if(scene.sourceAudio) {
            const QString start=QString::number(scene.clipStart,'f',9);
            const QString end=QString::number(scene.clipStart+scene.duration,'f',9);
            const QString duration=QString::number(double(scene.frameCount())/scene.fps,'f',9);
            // Keep source timestamps relative to the container's start, just as
            // the video seek does. Subtract the clip origin, not STARTPTS: the
            // latter would move a late-starting audio stream to the first frame.
            // A finite silence bed also covers an audio track that ends before
            // this clip; it never extends or truncates the rendered video.
            args<<"-ss"<<start<<"-protocol_whitelist"<<"file,pipe"<<"-i"<<scene.sourcePath;
            const QString filter=QString("[1:a:0]atrim=start=%1:end=%2,asetpts=PTS-%1/TB,aresample=48000:async=0:first_pts=0,aformat=channel_layouts=stereo[src];anullsrc=r=48000:cl=stereo,atrim=duration=%3[bed];[bed][src]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,atrim=duration=%3[audio]").arg(start,end,duration);
            args<<"-filter_complex_threads"<<"1"<<"-filter_complex"<<filter<<"-map"<<"0:v:0"<<"-map"<<"[audio]"<<"-c:a"<<"aac"<<"-ar"<<"48000"<<"-ac"<<"2"<<"-b:a"<<"192k";
        } else args<<"-map"<<"0:v:0"<<"-an";
        args<<"-map_metadata"<<"-1"<<"-map_chapters"<<"-1"<<"-c:v"<<"libx264"<<"-preset"<<"fast"<<"-crf"<<"18"<<"-pix_fmt"<<"yuv420p"<<"-threads"<<"2"<<"-movflags"<<"+faststart"<<"-f"<<"mp4"<<"-y"<<movie.temporary.fileName();
        if(!encoder.start(ffmpeg,args,error))return 1;
    }
    Demo::Renderer renderer(scene);QImage frame(scene.sourceWidth,scene.sourceHeight,QImage::Format_RGBA8888);
    if(frame.isNull()){error="Could not allocate the source frame.";return 1;}
    QImage posterFrame;const int count=time?1:scene.frameCount();
    for(int i=0;i<count;++i) {
        if(!readFrame(decoder,frame,error))return 1;
        QImage rendered=renderer.render(frame,time.value_or(scene.frameTime(i)),&error);if(rendered.isNull())return 1;
        if(i==0)posterFrame=rendered;
        if(time) {if(!rendered.save(movie.temporary.fileName(),"PNG")){error="Could not save the rendered preview.";return 1;}}
        else {const auto rgba=rendered.convertToFormat(QImage::Format_RGBA8888);if(!writeFrame(encoder,rgba,error))return 1;}
        if(i==0 || i+1==count || (i+1)%scene.fps==0)progress({{"phase","rendering"},{"frame",i+1},{"frames",count}});
    }
    if(!waitFinished(decoder,10000,error))return 1;
    if(!time){encoder.process.closeWriteChannel();if(!waitFinished(encoder,60000,error))return 1;if(!posterFrame.save(poster.temporary.fileName(),"PNG")){error="Could not save the export poster.";return 1;}}
    QJsonObject metadata{{"format","tomacco.demo.render"},{"version",1},{"output",output},{"width",scene.width},{"height",scene.height},{"frames",count},{"fps",scene.fps},{"duration",time?0:double(count)/scene.fps},{"requested_duration",scene.duration},{"silent",bool(time) || !scene.sourceAudio},{"audio",scene.sourceAudio && !time ? QJsonObject{{"mode","source"},{"codec","aac"},{"sample_rate",48000},{"channels",2}} : QJsonObject{{"mode","muted"}}},{"source_sha256",scene.sourceDigest},{"source_frame_selection","fixed-rate samples from clip start; repeated frames are held, never interpolated"}};
    if(time)metadata["time"]=*time;
    else {metadata["poster"]=poster.destination;metadata["manifest"]=manifest.destination;if(!manifest.writeJson(metadata,error))return 1;}
    progress({{"phase","publishing"},{"frames",count}});
    if(!movie.publish(error) || (!time && (!poster.publish(error) || !manifest.publish(error))))return 1;
    if(interrupted){error="Rendering canceled.";return 1;}
    movie.retain=poster.retain=manifest.retain=true;
    const auto bytes=QJsonDocument(metadata).toJson(QJsonDocument::Compact);std::fwrite(bytes.constData(),1,size_t(bytes.size()),stdout);std::fputc('\n',stdout);return 0;
}
}
int main(int argc,char **argv) {
    // This executable always renders locally offscreen. It never initializes the
    // desktop app, a device backend, a browser or a platform display connection.
    qputenv("QT_QPA_PLATFORM","offscreen");qputenv("QT_QPA_PLATFORMTHEME","generic");QGuiApplication app(argc,argv);QCoreApplication::setApplicationName("tomacco-demo-render");QImageReader::setAllocationLimit(128);
    std::signal(SIGINT,cancel);std::signal(SIGTERM,cancel);std::signal(SIGPIPE,SIG_IGN);
    QCommandLineParser parser;parser.setApplicationDescription("Render a local recorded video in a perspective 3D device scene.");parser.addHelpOption();
    parser.addOption({"scene","Absolute local scene JSON path.","path"});parser.addOption({"output","New artifact path (PNG with --time, MP4 otherwise).","path"});parser.addOption({"time","Render one preview at this scene time in seconds.","seconds"});parser.process(app);
    if(!parser.isSet("scene") || !parser.isSet("output"))parser.showHelp(2);
    QString error;std::optional<double> time;
    if(parser.isSet("time")){bool ok=false;const double value=parser.value("time").toDouble(&ok);if(!ok || !std::isfinite(value)){progress({{"error","Invalid preview time."}});return 2;}time=value;}
    const int result=run(parser.value("scene"),parser.value("output"),time,error);if(result)progress({{"error",error},{"canceled",bool(interrupted)}});return interrupted?130:result;
}
