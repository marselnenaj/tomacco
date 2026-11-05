#pragma once

#include <QColor>
#include <QImage>
#include <QJsonObject>
#include <QVector>
#include <QString>
#include <optional>

namespace Demo {
struct Pose {
    double time=0, yaw=0, pitch=0, roll=0, scale=.72, x=.5, y=.5;
    QString easing="smooth";
};
struct Scene {
    QString sourcePath, sourceDigest, backgroundImage, title;
    int sourceWidth=0, sourceHeight=0, sourceRotation=0, width=0, height=0, fps=0;
    double sourceDuration=0, clipStart=0, duration=0;
    QString model, backgroundStyle;
    QColor finish, background, background2;
    double bezel=.035, thickness=.065, cornerRadius=.09;
    bool shadow=true;
    double shadowOpacity=.22, shadowBlur=24, shadowX=0, shadowY=40;
    double distance=4, fov=35;
    QVector<Pose> keyframes;
    static std::optional<Scene> fromJson(const QJsonObject &document, QString *error=nullptr);
    int frameCount() const;
    double frameTime(int index) const;
    Pose poseAt(double time) const;
};
// This renderer has no device, network, Qt Quick or WebEngine dependencies.
// Source images use display orientation and retain their full aspect ratio.
class Renderer {
public:
    explicit Renderer(Scene scene);
    QImage render(const QImage &source, double time, QString *error=nullptr) const;
private:
    Scene m_scene;
    QImage m_background;
    QString m_backgroundError;
};
}
