#include "control.h"
#include "backend.h"
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QJSValue>
#include <QProcess>
#include <QRegularExpression>
#include <QTimer>
#include <QQuickWindow>
#include <QSaveFile>
#include <cmath>
#include <memory>
#include <utility>
#include <unistd.h>

// Restricts a directory to its owner and checks the result again: in /tmp
// another user may have created it between the first check and mkpath.
static bool ownerOnlyDirectory(const QString &path) {
    QFile::setPermissions(path,QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner);
    const QFileInfo info(path);
    constexpr auto others=QFile::ReadGroup|QFile::WriteGroup|QFile::ExeGroup|QFile::ReadOther|QFile::WriteOther|QFile::ExeOther;
    return info.isDir() && !info.isSymLink() && info.ownerId()==getuid() && !(info.permissions()&others);
}

// What tomaccoctl runs with for this window: it acts for the person in front
// of it, so the agent policy, desktop approval and the audit log, which are
// for agents, leave it alone.
QProcessEnvironment Control::personEnvironment() {
    auto environment=QProcessEnvironment::systemEnvironment();
    environment.insert(QStringLiteral("TOMACCO_CALLER"),QStringLiteral("desktop"));
    return environment;
}

Control::Control(const QString &session, QObject *parent, const QString &cli): QObject(parent), m_session(session), m_cli(cli) {
    // tomaccoctl is installed and built next to the desktop (bin/, build-product/).
    if(m_cli.isEmpty()) m_cli=QCoreApplication::applicationDirPath()+"/tomaccoctl";
    if(!QFileInfo(m_cli).isExecutable())
        m_cliError=tr("Tomacco's control program (tomaccoctl) was not found. Reinstall Tomacco or run install.py again.");
    const auto data=QDir::cleanPath(QFileInfo(Backend::dataDirectory()).absoluteFilePath());
    auto runtime=data+"/runtime";
    if(runtime.toUtf8().size()+54>=104) {
        const auto hash=QCryptographicHash::hash(data.toUtf8(),QCryptographicHash::Sha256).toHex().left(16);
        runtime=QStringLiteral("/tmp/tomacco-%1-%2").arg(getuid()).arg(QString::fromLatin1(hash));
    }
    if(!QRegularExpression("^[A-Za-z0-9_-]{1,40}$").match(session).hasMatch()) {
        m_socketError=tr("Invalid session name");
    } else if(QFileInfo(runtime).isSymLink() || (QFileInfo::exists(runtime) && QFileInfo(runtime).ownerId()!=getuid())) {
        m_socketError=tr("The agent socket directory belongs to another user");
    } else if(!(QFileInfo::exists(data) || (QDir().mkpath(data) && QFile::setPermissions(data,QFile::ReadOwner|QFile::WriteOwner|QFile::ExeOwner)))
              || !QDir().mkpath(runtime)) {
        // A new data directory is owner-only: it holds the job database and logs.
        m_socketError=tr("The agent socket directory could not be created");
    } else if(!ownerOnlyDirectory(runtime)) {
        m_socketError=tr("The agent socket directory belongs to another user");
    } else {
        m_socketPath=runtime+"/preview-"+session+".sock";
        QLocalSocket existing;
        existing.connectToServer(m_socketPath);
        if(existing.waitForConnected(100)) {
            m_socketError=tr("Tomacco is already open in another window; agents keep controlling that one. To control this window too, start it with --preview-session and another name.");
        } else {
            QLocalServer::removeServer(m_socketPath);
            m_server.setSocketOptions(QLocalServer::UserAccessOption);
            if(!m_server.listen(m_socketPath)) m_socketError=m_server.errorString();
            connect(&m_server,&QLocalServer::newConnection,this,&Control::accept);
        }
    }
    // At once, so tomaccoctl answers while the window is still being built.
    // A program that is missing answers on the next turn of the event loop, as before.
    if(m_cliError.isEmpty()) request("schema");
    else QTimer::singleShot(0,this,[this]{ request("schema"); });
}
Control::~Control() {
    if(m_server.isListening()) m_server.close();
    if(m_previewProcess) {
        // This child serves guest frames and live touches, never app jobs.
        m_previewProcess->disconnect(this);
        m_previewProcess->kill();
        m_previewProcess->waitForFinished(1000);
    }
    if(!m_touch.isEmpty()) {
        // A contact it may have held is lifted before the app is gone.
        QProcess release;
        release.setWorkingDirectory(QFileInfo(m_cli).absolutePath());
        release.setProcessEnvironment(personEnvironment());
        release.start(m_cli,{"call","ios.emulator.control","@-"});
        release.write(QJsonDocument(QJsonObject{{"job_id",m_touch.value("job_id").toString()},{"action","touch_up"}}).toJson(QJsonDocument::Compact));
        release.closeWriteChannel();
        if(!release.waitForFinished(5000))release.kill();
        release.waitForFinished(1000);
    }
}

QVariantMap Control::captureWindow(QObject *object,const QString &path,double scale) {
    auto *window=qobject_cast<QQuickWindow*>(object);
    if(!window || !QFileInfo(path).isAbsolute() || !path.endsWith(".png") || !std::isfinite(scale) || scale<1 || scale>3)
        return {{"error","Invalid window capture"}};
    const QSize size(qRound(window->width()*scale),qRound(window->height()*scale));
    if(size.isEmpty() || qint64(size.width())*size.height()>25000000)
        return {{"error","Screenshot too large"}};
    auto image=window->grabWindow();
    if(image.isNull())return {{"error","The window is not ready for a capture yet"}};
    if(image.size()!=size)image=image.scaled(size,Qt::IgnoreAspectRatio,Qt::SmoothTransformation);
    QSaveFile file(path);
    if(!file.open(QIODevice::WriteOnly) || !image.save(&file,"PNG") || !file.commit())
        return {{"error","The PNG could not be saved"}};
    return {{"path",path},{"width",image.width()},{"height",image.height()}};
}

int Control::request(const QString &operation,const QVariantMap &arguments) {
    const int id=++m_counter;
    const auto action=operation=="ios.emulator.control"?arguments.value("action").toString():QString();
    // A live touch is never refused here: the view keeps one in flight, and
    // a refused touch_up would leave the finger down.
    if(action.startsWith("touch_")) {
        if(action=="touch_up")m_touch.clear();
        else m_touch=arguments;
        requestLane(id,arguments,15000);
        return id;
    }
    if(m_running>=16) {
        QTimer::singleShot(0,this,[this,id,operation]{ emit apiFinished(id,operation,{{"ok",false},{"error",QVariantMap{{"message",tr("Too many concurrent requests")}}}}); });
        return id;
    }
    if(action=="frame" && arguments.value("transport").toString()=="file") {
        requestLane(id,arguments,90000);
        return id;
    }
    spawn(id,operation,arguments);
    return id;
}

void Control::spawn(int id,const QString &operation,const QVariantMap &arguments) {
    auto *process=new QProcess(this);
    ++m_running;
    auto done=std::make_shared<bool>(false);
    auto finish=[this,process,id,operation,done](const QString &failure) {
        if(*done)return;
        *done=true; --m_running;
        QJsonParseError error;
        const auto bytes=process->readAllStandardOutput();
        auto object=QJsonDocument::fromJson(bytes,&error).object();
        if(!failure.isEmpty() || error.error!=QJsonParseError::NoError || object.isEmpty()) {
            const QString detail=QString::fromUtf8(process->readAllStandardError()).trimmed().left(2000);
            object={{"ok",false},{"error",QJsonObject{{"code","control_plane_error"},
                {"message",!failure.isEmpty()?failure:detail.isEmpty()?tr("Invalid response from the control plane"):tr("The control program failed: %1").arg(detail)}}}};
        }
        if(operation=="schema") {
            if(object.value("ok").toBool()) {
                const auto schema=object.value("result").toObject();
                m_operations=schema.value("operations").toVariant().toList();
                emit operationsChanged();
                setCliState({},schema.value("version").toString());
            } else {
                // A slow or briefly failing start is retried a few times; a missing program is not.
                if(process->error()!=QProcess::FailedToStart && ++m_schemaAttempts<3)
                    QTimer::singleShot(5000,this,[this]{ request("schema"); });
                setCliState(process->error()==QProcess::FailedToStart
                    ?tr("Tomacco's control program (tomaccoctl) could not be started. Reinstall Tomacco or run install.py again.")
                    :tr("Tomacco's control program (tomaccoctl) is not working: %1").arg(object.value("error").toObject().value("message").toString()),{});
            }
        }
        emit apiFinished(id,operation,object.toVariantMap());
        process->deleteLater();
    };
    connect(process,qOverload<int,QProcess::ExitStatus>(&QProcess::finished),this,[finish](int,QProcess::ExitStatus status){ finish(status==QProcess::NormalExit?QString():tr("The control program ended unexpectedly")); });
    connect(process,&QProcess::errorOccurred,this,[this,finish,process](QProcess::ProcessError error){
        if(error==QProcess::FailedToStart)
            finish(tr("tomaccoctl could not be started (%1). Reinstall Tomacco or run install.py again.").arg(process->errorString()));
    });
    QTimer::singleShot(90000,process,[process]{ process->kill(); });
    process->setWorkingDirectory(QFileInfo(m_cli).absolutePath());
    process->setProcessEnvironment(personEnvironment());
    // Arguments travel on standard input, never in argv: argv is readable by
    // every local user through /proc and limited to 128 KiB per argument,
    // while a saved source file alone may be up to 1 MiB.
    process->start(m_cli,operation=="schema"?QStringList{"schema"}:QStringList{"call",operation,"@-"});
    if(operation!="schema") process->write(QJsonDocument(QJsonObject::fromVariantMap(arguments)).toJson(QJsonDocument::Compact));
    process->closeWriteChannel();
}

void Control::setCliState(const QString &error,const QString &version) {
    if(error==m_cliError && version==m_cliVersion)return;
    m_cliError=error; m_cliVersion=version;
    emit cliChanged();
}

void Control::requestLane(int id,const QVariantMap &arguments,int timeout) {
    auto *deadline=new QTimer(this);
    deadline->setSingleShot(true);
    m_previewRequests.insert(id,{arguments,deadline});
    ++m_running;
    connect(deadline,&QTimer::timeout,this,[this]{
        failPreviewTransport(tr("The frame control did not respond in time"));
    });
    deadline->start(timeout);
    if(!m_previewProcess) startPreviewTransport();
    else if(m_previewReady) sendPreviewFrame(id);
}

void Control::startPreviewTransport() {
    auto *process=new QProcess(this);
    m_previewProcess=process;
    m_previewReady=false;
    m_previewOutput.clear();
    process->setWorkingDirectory(QFileInfo(m_cli).absolutePath());
    connect(process,&QProcess::started,this,[this,process]{
        if(m_previewProcess!=process)return;
        const QJsonObject initialize{{"jsonrpc","2.0"},{"id","ios-preview-init"},
            {"method","initialize"},{"params",QJsonObject{{"protocolVersion","2025-11-25"},
                {"capabilities",QJsonObject{}},{"clientInfo",QJsonObject{{"name","tomacco-preview"},{"version","1"}}}}}};
        const auto message=QJsonDocument(initialize).toJson(QJsonDocument::Compact)+'\n';
        if(process->write(message)!=message.size())failPreviewTransport(tr("The frame control is not reachable"));
    });
    connect(process,&QProcess::readyReadStandardOutput,this,[this,process]{
        if(m_previewProcess==process)readPreviewResponses();
    });
    connect(process,&QProcess::readyReadStandardError,this,[process]{process->readAllStandardError();});
    connect(process,qOverload<int,QProcess::ExitStatus>(&QProcess::finished),this,[this,process]{
        if(m_previewProcess==process)failPreviewTransport(tr("The frame control ended"));
    });
    connect(process,&QProcess::errorOccurred,this,[this,process](QProcess::ProcessError error){
        if(m_previewProcess==process && error==QProcess::FailedToStart)
            failPreviewTransport(process->errorString());
    });
    process->setProcessEnvironment(personEnvironment());
    process->start(m_cli,{"mcp"});
}

void Control::sendPreviewFrame(int id) {
    if(!m_previewProcess || !m_previewReady || !m_previewRequests.contains(id))return;
    const QJsonObject request{{"jsonrpc","2.0"},{"id",id},{"method","tools/call"},
        {"params",QJsonObject{{"name","ios.emulator.control"},
            {"arguments",QJsonObject::fromVariantMap(m_previewRequests.value(id).arguments)}}}};
    const auto message=QJsonDocument(request).toJson(QJsonDocument::Compact)+'\n';
    if(m_previewProcess->write(message)!=message.size())
        failPreviewTransport(tr("The frame request could not be sent"));
}

void Control::readPreviewResponses() {
    m_previewOutput+=m_previewProcess->readAllStandardOutput();
    // File-frame replies contain paths and metadata, not encoded image bytes.
    constexpr qsizetype limit=2*1024*1024;
    while(true) {
        const auto newline=m_previewOutput.indexOf('\n');
        if(newline<0) {
            if(m_previewOutput.size()>limit)failPreviewTransport(tr("The frame response is too large"));
            return;
        }
        if(newline>limit) {failPreviewTransport(tr("The frame response is too large"));return;}
        const auto line=m_previewOutput.left(newline);
        m_previewOutput.remove(0,newline+1);
        QJsonParseError error;
        const auto response=QJsonDocument::fromJson(line,&error).object();
        if(error.error!=QJsonParseError::NoError || response.value("jsonrpc")!="2.0") {
            failPreviewTransport(tr("Invalid response from the frame control"));return;
        }
        if(response.value("id")=="ios-preview-init" && !m_previewReady) {
            const auto initialized=response.value("result").toObject();
            if(initialized.value("protocolVersion")!="2025-11-25" || !initialized.value("capabilities").isObject()) {
                failPreviewTransport(tr("The frame control could not be initialized"));return;
            }
            m_previewReady=true;
            const auto notification=QJsonDocument(QJsonObject{{"jsonrpc","2.0"},
                {"method","notifications/initialized"}}).toJson(QJsonDocument::Compact)+'\n';
            if(m_previewProcess->write(notification)!=notification.size()) {
                failPreviewTransport(tr("The frame control is not reachable"));return;
            }
            const auto ids=m_previewRequests.keys();
            const QPointer<Control> alive(this);
            for(const auto id:ids) {
                sendPreviewFrame(id);
                if(!alive || !m_previewProcess)return;
            }
            continue;
        }
        const auto rpcId=response.value("id");
        const int id=rpcId.toInt(-1);
        if(!rpcId.isDouble() || rpcId.toDouble()!=double(id) || !m_previewRequests.contains(id)) {
            failPreviewTransport(tr("The frame response matches no request"));return;
        }
        QJsonObject envelope;
        const auto result=response.value("result").toObject();
        const auto content=result.value("content").toArray();
        if(response.value("error").isObject()) {
            envelope={{"ok",false},{"error",QJsonObject{{"code","control_plane_error"},
                {"message",response.value("error").toObject().value("message")}}}};
        } else if(content.size()==1 && content[0].toObject().value("type")=="text"
                  && result.value("isError").isBool()) {
            const auto payload=QJsonDocument::fromJson(content[0].toObject().value("text").toString().toUtf8(),&error);
            if(error.error==QJsonParseError::NoError && payload.isObject()) {
                if(result.value("isError").toBool() && payload.object().value("error").isObject())
                    envelope={{"ok",false},{"error",payload.object().value("error")}};
                else if(!result.value("isError").toBool())envelope={{"ok",true},{"result",payload.object()}};
            }
        }
        if(envelope.isEmpty()) {failPreviewTransport(tr("Invalid frame response"));return;}
        const auto pending=m_previewRequests.take(id);
        if(pending.deadline) {pending.deadline->stop();pending.deadline->deleteLater();}
        --m_running;
        const QPointer<Control> alive(this);
        emit apiFinished(id,"ios.emulator.control",envelope.toVariantMap());
        if(!alive || !m_previewProcess)return;
    }
}

void Control::failPreviewTransport(const QString &message) {
    auto process=m_previewProcess;
    m_previewProcess=nullptr;
    m_previewReady=false;
    m_previewOutput.clear();
    const auto pending=std::exchange(m_previewRequests,{});
    m_running-=pending.size();
    if(process) {
        process->disconnect(this);
        process->kill();
        process->deleteLater();
    }
    liftTouch();
    const QPointer<Control> alive(this);
    for(auto it=pending.cbegin();it!=pending.cend();++it) {
        if(it.value().deadline) {it.value().deadline->stop();it.value().deadline->deleteLater();}
        emit apiFinished(it.key(),"ios.emulator.control",{{"ok",false},{"error",QVariantMap{
            {"code","control_plane_error"},{"message",message}}}});
        if(!alive)return;
    }
    // A later frame may create a new child. Never replay a submitted request.
}

// The child ended, perhaps holding a contact down (it lifts what it holds
// only when it ends on its own): a one-shot release, sent like any request,
// lifts it in the guest. Its reply belongs to no view.
void Control::liftTouch() {
    if(m_touch.isEmpty())return;
    QVariantMap release{{"job_id",m_touch.value("job_id")},{"action","touch_up"}};
    m_touch.clear();
    spawn(++m_counter,"ios.emulator.control",release);
}

void Control::accept() {
    while(m_server.hasPendingConnections()) {
        auto *socket=m_server.nextPendingConnection();
        if(m_clients.size()>=32) { socket->disconnectFromServer(); socket->deleteLater(); continue; }
        const int id=++m_counter;
        m_clients[id]=socket;
        connect(socket,&QLocalSocket::disconnected,this,[this,socket,id]{m_clients.remove(id);m_ids.remove(id);socket->deleteLater();});
        auto buffer=std::make_shared<QByteArray>();
        auto consumed=std::make_shared<bool>(false);
        QTimer::singleShot(35000,socket,[this,id]{complete(id,{},"The preview did not respond in time");});
        connect(socket,&QLocalSocket::readyRead,this,[this,socket,id,buffer,consumed]{
            if(*consumed)return;
            *buffer+=socket->readAll();
            if(buffer->size()>1024*1024) { complete(id,{},"Request too large"); return; }
            if(!buffer->contains('\n'))return;
            *consumed=true;
            QJsonParseError error;
            const auto doc=QJsonDocument::fromJson(buffer->left(buffer->indexOf('\n')),&error);
            const auto object=doc.object();
            m_ids[id]=object.value("id").toVariant();
            if(error.error!=QJsonParseError::NoError || !object.value("method").isString() || !object.value("params").isObject()) {
                complete(id,{},"Invalid preview request"); return;
            }
            emit previewRequest(id,object.value("method").toString(),object.value("params").toObject().toVariantMap());
        });
    }
}

void Control::complete(int id,const QVariant &result,const QString &error) {
    emit previewFinished(id,error);
    auto socket=m_clients.value(id);
    if(!socket)return;
    QJsonObject response{{"id",QJsonValue::fromVariant(m_ids.value(id))}};
    if(error.isEmpty())response["result"]=QJsonValue::fromVariant(result.metaType()==QMetaType::fromType<QJSValue>()?result.value<QJSValue>().toVariant():result);
    else response["error"]=QJsonObject{{"code","preview_error"},{"message",error}};
    socket->write(QJsonDocument(response).toJson(QJsonDocument::Compact)+"\n");
    socket->flush(); socket->disconnectFromServer();
    m_clients.remove(id);m_ids.remove(id);
}
