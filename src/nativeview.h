#pragma once
#include <QQuickItem>
#include <QImage>
#include <QThread>
#include <QLocalSocket>
#include <QQueue>
#include <QJsonObject>
#include <QTimer>

class VideoDecoder;
class NativeView : public QQuickItem {
    Q_OBJECT
    Q_PROPERTY(QVariantMap session READ session WRITE setSession NOTIFY sessionChanged)
    Q_PROPERTY(QString state READ state NOTIFY stateChanged)
    Q_PROPERTY(QString error READ error NOTIFY stateChanged)
    Q_PROPERTY(QSize sourceSize READ sourceSize NOTIFY sourceSizeChanged)
    Q_PROPERTY(QRectF contentRect READ contentRect NOTIFY contentRectChanged)
    Q_PROPERTY(quint64 frameCount READ frameCount NOTIFY frameChanged)
    Q_PROPERTY(double framesPerSecond READ framesPerSecond NOTIFY statisticsChanged)
public:
    explicit NativeView(QQuickItem *parent=nullptr);
    ~NativeView() override;
    QVariantMap session() const {return m_session;}
    void setSession(const QVariantMap &session);
    QString state() const {return m_state;}
    QString error() const {return m_error;}
    QSize sourceSize() const {return m_image.size();}
    QRectF contentRect() const;
    quint64 frameCount() const {return m_frames;}
    double framesPerSecond() const {return m_fps;}
    Q_INVOKABLE void sendInput(QVariantMap arguments);
    Q_INVOKABLE void insertText(const QString &text);
signals:
    void sessionChanged();
    void stateChanged();
    void frameChanged();
    void sourceSizeChanged();
    void contentRectChanged();
    void statisticsChanged();
    void inputError(const QString &message);
protected:
    QSGNode *updatePaintNode(QSGNode *,UpdatePaintNodeData *) override;
    void geometryChange(const QRectF &,const QRectF &) override;
    void mousePressEvent(QMouseEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mouseReleaseEvent(QMouseEvent *) override;
    void mouseUngrabEvent() override;
    void keyPressEvent(QKeyEvent *) override;
    void wheelEvent(QWheelEvent *) override;
private:
    void touch(const QPointF &,const QString &);
    void flushWheel();
    void finishWheel();
    void nextInput();
    void finishInput();
    QThread m_thread;
    VideoDecoder *m_decoder;
    QVariantMap m_session;
    QString m_state="disconnected",m_error;
    QImage m_image;
    bool m_dirty=false,m_pressed=false,m_hasTouch=false;
    QPointF m_lastPoint,m_lastTarget;
    quint64 m_frames=0,m_previousFrames=0;
    quint64 m_generation=0;
    double m_fps=0;
    QQueue<QJsonObject> m_inputs;
    QLocalSocket m_control;
    QTimer m_inputTimeout,m_statistics,m_wheelFrame,m_wheelEnd;
    QPointF m_wheelPoint,m_wheelDelta;
    bool m_wheelActive=false;
    QByteArray m_response;
};
