#pragma once
#include <QObject>
#include <QLocalServer>
#include <QLocalSocket>
#include <QPointer>
#include <QVariantList>
#include <QHash>

class QProcess;
class QTimer;

class Control final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList operations READ operations NOTIFY operationsChanged)
    Q_PROPERTY(QString session READ session CONSTANT)
    Q_PROPERTY(QString socketError READ socketError CONSTANT)
    Q_PROPERTY(QString cliPath READ cliPath CONSTANT)
    // Empty while tomaccoctl answers; otherwise a sentence for the user.
    Q_PROPERTY(QString cliError READ cliError NOTIFY cliChanged)
    Q_PROPERTY(QString cliVersion READ cliVersion NOTIFY cliChanged)
public:
    // cli: the tomaccoctl to use; by default the one next to the desktop binary.
    explicit Control(const QString &session, QObject *parent=nullptr, const QString &cli={});
    ~Control() override;
    QVariantList operations() const { return m_operations; }
    QString session() const { return m_session; }
    QString socketError() const { return m_socketError; }
    QString cliPath() const { return m_cli; }
    QString cliError() const { return m_cliError; }
    QString cliVersion() const { return m_cliVersion; }
    Q_INVOKABLE int request(const QString &operation, const QVariantMap &arguments={});
    Q_INVOKABLE void complete(int requestId, const QVariant &result, const QString &error={});
    Q_INVOKABLE QVariantMap captureWindow(QObject *window, const QString &path, double scale=1);
signals:
    void operationsChanged();
    void cliChanged();
    void apiFinished(int requestId, const QString &operation, const QVariantMap &response);
    void previewRequest(int requestId, const QString &method, const QVariantMap &parameters);
    // A preview request answered: error is empty when it worked.
    void previewFinished(int requestId, const QString &error);
private:
    void accept();
    void spawn(int requestId, const QString &operation, const QVariantMap &arguments);
    // Frames and live touches share one persistent tomaccoctl mcp child.
    void requestLane(int requestId, const QVariantMap &arguments, int timeout);
    void liftTouch();
    void startPreviewTransport();
    void readPreviewResponses();
    void failPreviewTransport(const QString &message);
    void sendPreviewFrame(int requestId);
    struct PreviewRequest {
        QVariantMap arguments;
        QPointer<QTimer> deadline;
    };
    QPointer<QProcess> m_previewProcess;
    QHash<int,PreviewRequest> m_previewRequests;
    QByteArray m_previewOutput;
    bool m_previewReady=false;
    // The last touch_down/touch_move sent through that child until its
    // touch_up: the contact it may hold in the guest.
    QVariantMap m_touch;
    void setCliState(const QString &error, const QString &version);
    QString m_session, m_socketError, m_cli, m_socketPath, m_cliError, m_cliVersion;
    QVariantList m_operations;
    QLocalServer m_server;
    QHash<int,QPointer<QLocalSocket>> m_clients;
    QHash<int,QVariant> m_ids;
    int m_counter=0, m_running=0, m_schemaAttempts=0;
};
