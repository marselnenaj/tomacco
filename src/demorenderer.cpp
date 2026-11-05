#include "demorenderer.h"

#include <QCoreApplication>
#include <QFileInfo>
#include <QImageReader>
#include <QJsonArray>
#include <QLinearGradient>
#include <QPainter>
#include <QPainterPath>
#include <QPolygonF>
#include <QTransform>
#include <algorithm>
#include <cmath>
#include <numbers>
#include <utility>

namespace Demo {
namespace {
constexpr qint64 MaxPixels=32*1024*1024;
QString translated(const char *s) { return QCoreApplication::translate("DemoRenderer",s); }
bool number(const QJsonObject &o,const QString &key,double low,double high,double &value) {
    const auto v=o.value(key);if(!v.isDouble())return false;
    value=v.toDouble();return std::isfinite(value) && value>=low && value<=high;
}
bool integer(const QJsonObject &o,const QString &key,int low,int high,int &value) {
    double n=0;if(!number(o,key,low,high,n) || std::floor(n)!=n)return false;value=int(n);return true;
}
bool color(const QJsonObject &o,const QString &key,QColor &value) {
    if(!o.value(key).isString())return false;
    const QString text=o.value(key).toString();
    // The scene contract uses opaque RGB, with no CSS parsing or external URLs.
    if(text.size()!=7 || !text.startsWith('#'))return false;
    value=QColor(text);return value.isValid();
}
struct Vec {double x,y,z;};
Vec rotate(Vec p,const Pose &pose) {
    const double a=pose.yaw*std::numbers::pi/180,b=pose.pitch*std::numbers::pi/180,c=pose.roll*std::numbers::pi/180;
    p={p.x*std::cos(a)+p.z*std::sin(a),p.y,-p.x*std::sin(a)+p.z*std::cos(a)};
    p={p.x,p.y*std::cos(b)-p.z*std::sin(b),p.y*std::sin(b)+p.z*std::cos(b)};
    return {p.x*std::cos(c)-p.y*std::sin(c),p.x*std::sin(c)+p.y*std::cos(c),p.z};
}
QVector<QPointF> rounded(double w,double h,double r) {
    r=std::min({r,w/2,h/2});QVector<QPointF> result;
    const QPointF centers[]={{w/2-r,-h/2+r},{w/2-r,h/2-r},{-w/2+r,h/2-r},{-w/2+r,-h/2+r}};
    for(int corner=0;corner<4;++corner)for(int i=0;i<=12;++i) {
        const double a=(-90+corner*90+i*90./12)*std::numbers::pi/180;
        result.append(centers[corner]+QPointF(r*std::cos(a),r*std::sin(a)));
    }
    return result;
}
QPainterPath path(const QPolygonF &p) { QPainterPath result;result.addPolygon(p);result.closeSubpath();return result; }
void blurAlpha(QImage &image,int radius) {
    if(radius<=0)return;
    const int w=image.width(),h=image.height();QImage temp(w,h,QImage::Format_Grayscale8);
    // Two separable box passes, bounded by the quarter-resolution shadow image.
    for(int pass=0;pass<2;++pass) {
        for(int y=0;y<h;++y) {
            const auto *row=image.constScanLine(y);auto *dst=temp.scanLine(y);int sum=0;
            for(int dx=-radius;dx<=radius;++dx)sum+=row[std::clamp(dx,0,w-1)];
            for(int x=0;x<w;++x) {dst[x]=uchar(sum/(2*radius+1));sum+=row[std::clamp(x+radius+1,0,w-1)]-row[std::clamp(x-radius,0,w-1)];}
        }
        for(int x=0;x<w;++x) {
            int sum=0;for(int dy=-radius;dy<=radius;++dy)sum+=temp.constScanLine(std::clamp(dy,0,h-1))[x];
            for(int y=0;y<h;++y) {image.scanLine(y)[x]=uchar(sum/(2*radius+1));sum+=temp.constScanLine(std::clamp(y+radius+1,0,h-1))[x]-temp.constScanLine(std::clamp(y-radius,0,h-1))[x];}
        }
    }
}
}
std::optional<Scene> Scene::fromJson(const QJsonObject &doc,QString *error) {
    const auto fail=[&](const char *message)->std::optional<Scene>{if(error)*error=translated(message);return std::nullopt;};
    if(doc.value("format")!="tomacco.demo" || doc.value("version").toInt()!=1)return fail("Unsupported demo scene format or version.");
    Scene s;s.title=doc.value("title").toString();const auto source=doc.value("source").toObject();
    s.sourcePath=source.value("path").toString();s.sourceDigest=source.value("sha256").toString();
    if(!QFileInfo(s.sourcePath).isAbsolute() || !QFileInfo(s.sourcePath).isFile())return fail("The source must be an existing absolute local video path.");
    if(!integer(source,"width",1,16384,s.sourceWidth) || !integer(source,"height",1,16384,s.sourceHeight) || qint64(s.sourceWidth)*s.sourceHeight>MaxPixels || !number(source,"duration",.1,86400,s.sourceDuration))return fail("Invalid source dimensions or duration (maximum 32 megapixels).");
    if(!integer(source,"rotation",0,270,s.sourceRotation) || s.sourceRotation%90)return fail("Source rotation must be 0, 90, 180 or 270 degrees.");
    const auto clip=doc.value("clip").toObject(),out=doc.value("output").toObject();
    if(!number(clip,"start",0,s.sourceDuration,s.clipStart) || !integer(out,"width",320,3840,s.width) || !integer(out,"height",320,3840,s.height) || s.width%2 || s.height%2 || !integer(out,"fps",24,60,s.fps) || !QList<int>{24,25,30,60}.contains(s.fps) || !number(out,"duration",.1,60,s.duration) || s.clipStart+s.duration>s.sourceDuration+1e-6)return fail("Invalid output dimensions, frame rate, duration or source clip range.");
    const auto device=doc.value("device").toObject();s.model=device.value("model").toString();
    if(!QStringList{"phone","tablet"}.contains(s.model) || !color(device,"finish",s.finish) || !number(device,"bezel",.005,.12,s.bezel) || !number(device,"thickness",.01,.2,s.thickness) || !number(device,"corner_radius",.01,.2,s.cornerRadius))return fail("Invalid device model, finish or geometry.");
    const auto bg=doc.value("background").toObject();s.backgroundStyle=bg.value("style").toString();s.backgroundImage=bg.value("image").toString();
    if(!QStringList{"solid","gradient","image"}.contains(s.backgroundStyle) || !color(bg,"color",s.background) || !color(bg,"color2",s.background2))return fail("Invalid background style or colors.");
    if(s.backgroundStyle=="image" && (!QFileInfo(s.backgroundImage).isAbsolute() || !QFileInfo(s.backgroundImage).isFile()))return fail("The background image must be an existing absolute local path.");
    const auto sh=doc.value("shadow").toObject();
    if(!sh.value("enabled").isBool() || !number(sh,"opacity",0,1,s.shadowOpacity) || !number(sh,"blur",0,100,s.shadowBlur) || !number(sh,"offset_x",-500,500,s.shadowX) || !number(sh,"offset_y",-500,500,s.shadowY))return fail("Invalid shadow parameters.");s.shadow=sh.value("enabled").toBool();
    const auto cam=doc.value("camera").toObject();
    if(!number(cam,"distance",2,12,s.distance) || !number(cam,"fov",15,75,s.fov))return fail("Invalid camera distance or field of view.");
    const auto keys=doc.value("keyframes").toArray();if(keys.isEmpty() || keys.size()>120)return fail("A scene needs between 1 and 120 pose keyframes.");
    double last=-1;
    for(const auto &value:keys) {
        if(!value.isObject())return fail("Invalid pose keyframe.");const auto k=value.toObject();Pose p;p.easing=k.value("easing").toString();
        if(!number(k,"time",0,s.duration,p.time) || p.time<=last || !number(k,"yaw",-75,75,p.yaw) || !number(k,"pitch",-75,75,p.pitch) || !number(k,"roll",-180,180,p.roll) || !number(k,"scale",.1,1.5,p.scale) || !number(k,"x",0,1,p.x) || !number(k,"y",0,1,p.y) || !QStringList{"linear","smooth","ease-in","ease-out"}.contains(p.easing))return fail("Invalid keyframe time, pose or easing.");
        last=p.time;s.keyframes.append(p);
    }
    return s;
}
int Scene::frameCount() const {return int(std::ceil(duration*fps));}
double Scene::frameTime(int i) const {return double(i)/fps;}
Pose Scene::poseAt(double time) const {
    if(time<=keyframes.first().time)return keyframes.first();if(time>=keyframes.last().time)return keyframes.last();
    for(int i=1;i<keyframes.size();++i)if(time<=keyframes[i].time) {
        const auto &a=keyframes[i-1],&b=keyframes[i];double t=(time-a.time)/(b.time-a.time);
        if(a.easing=="smooth")t=t*t*(3-2*t);else if(a.easing=="ease-in")t*=t;else if(a.easing=="ease-out")t=1-(1-t)*(1-t);
        const auto mix=[&](double x,double y){return x+(y-x)*t;};
        Pose p;p.time=time;p.yaw=mix(a.yaw,b.yaw);p.pitch=mix(a.pitch,b.pitch);p.roll=mix(a.roll,b.roll);p.scale=mix(a.scale,b.scale);p.x=mix(a.x,b.x);p.y=mix(a.y,b.y);p.easing=a.easing;return p;
    }
    return keyframes.last();
}
Renderer::Renderer(Scene scene):m_scene(std::move(scene)) {
    if(m_scene.backgroundStyle=="image") {
        QImageReader reader(m_scene.backgroundImage);reader.setAutoTransform(true);
        const QSize size=reader.size();
        if(!size.isValid() || qint64(size.width())*size.height()>MaxPixels)m_backgroundError="The background image exceeds 32 megapixels or has invalid dimensions.";
        else {reader.setScaledSize(size.scaled(m_scene.width,m_scene.height,Qt::KeepAspectRatio));m_background=reader.read();if(m_background.isNull())m_backgroundError="The background image could not be decoded.";}
    }
}
QImage Renderer::render(const QImage &source,double time,QString *error) const {
    const auto fail=[&](const char *message){if(error)*error=translated(message);return QImage();};
    if(!std::isfinite(time) || time<0 || time>m_scene.duration)return fail("Preview time is outside the scene duration.");
    if(source.isNull() || source.width()!=m_scene.sourceWidth || source.height()!=m_scene.sourceHeight)return fail("The decoded source frame does not match the scene display dimensions.");
    if(!m_backgroundError.isEmpty()){if(error)*error=m_backgroundError;return {};}
    QImage result(m_scene.width,m_scene.height,QImage::Format_ARGB32_Premultiplied);if(result.isNull())return fail("Could not allocate the output frame.");
    QPainter painter(&result);painter.setRenderHints(QPainter::Antialiasing|QPainter::SmoothPixmapTransform);
    if(m_scene.backgroundStyle=="gradient") {QLinearGradient gradient(0,0,m_scene.width,m_scene.height);gradient.setColorAt(0,m_scene.background);gradient.setColorAt(1,m_scene.background2);painter.fillRect(result.rect(),gradient);}
    else painter.fillRect(result.rect(),m_scene.background);
    if(!m_background.isNull()) {
        const double factor=std::max(double(m_scene.width)/m_background.width(),double(m_scene.height)/m_background.height());
        const QSizeF size(m_background.width()*factor,m_background.height()*factor);
        painter.drawImage(QRectF(QPointF((m_scene.width-size.width())/2,(m_scene.height-size.height())/2),size),m_background);
    }
    const auto pose=m_scene.poseAt(time);
    // A phone and tablet keep their device proportions. A mismatching recording
    // is fitted inside the screen with letterboxing, never stretched or cropped.
    double ratio=m_scene.model=="tablet"?3./4:9./19.5;
    if(source.width()>source.height())ratio=1/ratio;
    const double sw=ratio>1?2:2*ratio,sh=ratio>1?2/ratio:2;
    const double bw=sw+2*m_scene.bezel,bh=sh+2*m_scene.bezel,depth=m_scene.thickness;
    const double pixels=std::min(m_scene.width/bw,m_scene.height/bh)*pose.scale*(4/m_scene.distance)*std::tan(35*std::numbers::pi/360)/std::tan(m_scene.fov*std::numbers::pi/360);
    const auto project=[&](Vec v){v=rotate(v,pose);const double perspective=1-v.z/m_scene.distance;return QPointF(pose.x*m_scene.width+v.x*pixels/perspective,pose.y*m_scene.height+v.y*pixels/perspective);};
    const auto polygon=[&](const QVector<QPointF>&points,double z){QPolygonF p;for(auto v:points)p<<project({v.x(),v.y(),z});return p;};
    const auto edge=rounded(bw,bh,m_scene.cornerRadius),screenEdge=rounded(sw,sh,std::max(.002,m_scene.cornerRadius-m_scene.bezel));
    const auto front=polygon(edge,depth/2),back=polygon(edge,-depth/2);
    if(m_scene.shadow && m_scene.shadowOpacity>0) {
        constexpr int down=4;QImage alpha((result.width()+down-1)/down,(result.height()+down-1)/down,QImage::Format_Grayscale8);alpha.fill(0);
        {QPainter p(&alpha);p.setRenderHint(QPainter::Antialiasing);p.scale(1./down,1./down);p.translate(m_scene.shadowX,m_scene.shadowY);p.setPen(Qt::NoPen);p.setBrush(Qt::white);p.drawPath(path(front));}
        blurAlpha(alpha,int(std::ceil(m_scene.shadowBlur/down)));
        QImage shadow(alpha.size(),QImage::Format_ARGB32_Premultiplied);
        for(int y=0;y<alpha.height();++y){const auto *a=alpha.constScanLine(y);auto *out=reinterpret_cast<QRgb*>(shadow.scanLine(y));for(int x=0;x<alpha.width();++x)out[x]=qRgba(0,0,0,int(a[x]*m_scene.shadowOpacity));}
        painter.drawImage(result.rect(),shadow);
    }
    painter.setPen(Qt::NoPen);painter.setBrush(m_scene.finish.darker(170));painter.drawPath(path(back));
    struct Face {QPolygonF polygon;double z;QColor color;};QVector<Face> faces;
    for(int i=0;i<edge.size();++i) {
        const int next=(i+1)%edge.size();const QPointF middle=(edge[i]+edge[next])/2;
        const auto tangent=edge[next]-edge[i];const double length=std::hypot(tangent.x(),tangent.y());
        if(length<1e-9)continue;
        const Vec normal=rotate({tangent.y()/length,-tangent.x()/length,0},pose);
        const Vec center=rotate({middle.x(),middle.y(),0},pose);
        // Cull against the actual perspective camera, not an orthographic view.
        // The edge tangent supplies the outward normal, including rounded corners.
        if(normal.z*(m_scene.distance-center.z)-normal.x*center.x-normal.y*center.y<=0)continue;
        QColor lit=m_scene.finish.lighter(std::clamp(int(112+normal.x*24-normal.y*28+normal.z*18),75,160));
        faces.append({QPolygonF{back[i],back[next],front[next],front[i]},center.z,lit});
    }
    std::sort(faces.begin(),faces.end(),[](const Face&a,const Face&b){return a.z<b.z;});
    for(const auto &face:faces){painter.setBrush(face.color);painter.drawPolygon(face.polygon);}
    QLinearGradient metal(front.boundingRect().topLeft(),front.boundingRect().bottomRight());metal.setColorAt(0,m_scene.finish.lighter(145));metal.setColorAt(.5,m_scene.finish);metal.setColorAt(1,m_scene.finish.darker(130));
    painter.setBrush(metal);painter.setPen(QPen(m_scene.finish.lighter(180),.8));painter.drawPath(path(front));
    const auto screen=polygon(screenEdge,depth/2+.001);painter.setPen(Qt::NoPen);painter.setBrush(Qt::black);painter.drawPath(path(screen));
    const QPolygonF from{QPointF(0,0),QPointF(source.width(),0),QPointF(source.width(),source.height()),QPointF(0,source.height())};
    const double fit=std::min(sw/source.width(),sh/source.height());
    const double vw=source.width()*fit,vh=source.height()*fit;
    const QPolygonF to{project({-vw/2,-vh/2,depth/2+.002}),project({vw/2,-vh/2,depth/2+.002}),project({vw/2,vh/2,depth/2+.002}),project({-vw/2,vh/2,depth/2+.002})};
    QTransform transform;if(!QTransform::quadToQuad(from,to,transform))return fail("The camera pose could not be projected.");
    painter.save();painter.setClipPath(path(screen));painter.setTransform(transform);painter.drawImage(QPointF(0,0),source);painter.restore();
    // Thin edge highlight, with no notch or extra content covering the recording.
    painter.setBrush(Qt::NoBrush);painter.setPen(QPen(QColor(255,255,255,38),.7));painter.drawPath(path(screen));
    painter.end();return result;
}
}
