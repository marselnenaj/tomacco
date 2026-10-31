#include "nativeview.h"
#include <QSGSimpleTextureNode>
#include <QQuickWindow>
#include <QJsonDocument>
#include <QMutex>
#include <QSignalBlocker>
#include <QCoreApplication>
#include <QMutexLocker>
#include <QMouseEvent>
#include <QKeyEvent>
#include <QtEndian>
#include <algorithm>
#include <cstring>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
}

// All decoding and socket reads run off the GUI thread. Only the newest decoded
// frame crosses threads, bounding memory even when rendering is temporarily busy.
class VideoDecoder final : public QObject {
    Q_OBJECT
public:
    QImage takeFrame() {QMutexLocker lock(&mutex);notified=false;return latest;}
    void stop() {
        if(socket){socket->disconnect(this);socket->abort();socket->deleteLater();socket=nullptr;}
        avcodec_free_context(&codec);av_parser_close(parser);parser=nullptr;
        av_frame_free(&frame);av_frame_free(&pendingFrame);av_packet_free(&packet);sws_freeContext(scaler);scaler=nullptr;
        buffer.clear();prefix.clear();for(auto &image:pool)image={};QMutexLocker lock(&mutex);latest={};notified=false;
    }
    void start(const QString &path,quint64 generation,const QString &format,bool completeFrames) {
        stop();epoch=generation;if(path.isEmpty())return;
        jpeg=format=="mjpeg";framed=completeFrames;
        auto id=jpeg?AV_CODEC_ID_MJPEG:AV_CODEC_ID_H264;
        codec=avcodec_alloc_context3(avcodec_find_decoder(id));
        if(!jpeg && !framed)parser=av_parser_init(id);
        frame=av_frame_alloc();pendingFrame=av_frame_alloc();packet=av_packet_alloc();
        if(!codec || (!jpeg && !framed && !parser) || !frame || !pendingFrame || !packet){fail(QCoreApplication::translate("NativeView","The video decoder could not be initialized"));return;}
        codec->thread_count=2;codec->thread_type=FF_THREAD_SLICE;codec->flags|=AV_CODEC_FLAG_LOW_DELAY;
        if(avcodec_open2(codec,avcodec_find_decoder(id),nullptr)<0){fail(QCoreApplication::translate("NativeView","Video decoder not available"));return;}
        socket=new QLocalSocket(this);socket->setReadBufferSize(32*1024*1024);
        connect(socket,&QLocalSocket::readyRead,this,[this]{read();});
        connect(socket,&QLocalSocket::disconnected,this,[this]{fail(QCoreApplication::translate("NativeView","Connection to the runtime ended"));});
        connect(socket,&QLocalSocket::errorOccurred,this,[this]{fail(socket->errorString());});
        socket->connectToServer(path,QIODevice::ReadOnly);
    }
signals:
    void ready(quint64 generation);
    void failed(quint64 generation,const QString &message);
private:
    void fail(const QString &message) {emit failed(epoch,message);}
    void read() {
        buffer+=socket->readAll();
        qsizetype offset=0;
        while(buffer.size()-offset>=4) {
            auto size=qFromBigEndian<quint32>(reinterpret_cast<const uchar *>(buffer.constData()+offset));
            if(!size || size>16*1024*1024){fail(QCoreApplication::translate("NativeView","Invalid video packet size"));socket->abort();return;}
            if(buffer.size()-offset<qsizetype(size)+4)break;
            QByteArray data=buffer.mid(offset+4,size);offset+=size+4;
            if(framed && !jpeg) {
                bool picture=false;
                for(qsizetype i=0;i+3<data.size();++i) {
                    if(data[i]==0 && data[i+1]==0 && data[i+2]==1) {
                        int type=uchar(data[i+3])&31;
                        if(type>=1 && type<=5){picture=true;break;}
                    }
                }
                // scrcpy already provides complete access units. A streaming
                // parser would hold each picture until the next one arrived,
                // leaving an idle screen one frame behind the actual device.
                if(!picture) {if(prefix.size()+data.size()<=1024*1024)prefix+=data;else prefix.clear();continue;}
                if(!prefix.isEmpty()){data.prepend(prefix);prefix.clear();}
                size=data.size();
            }
            data.resize(size+AV_INPUT_BUFFER_PADDING_SIZE,0);
            auto *cursor=reinterpret_cast<uint8_t *>(data.data());int remaining=int(size);
            if(jpeg || framed){packet->data=cursor;packet->size=remaining;decode();continue;}
            while(remaining>0) {
                uint8_t *output=nullptr;int outputSize=0;
                int consumed=av_parser_parse2(parser,codec,&output,&outputSize,cursor,remaining,AV_NOPTS_VALUE,AV_NOPTS_VALUE,0);
                if(consumed<0){fail(QCoreApplication::translate("NativeView","The H.264 data could not be read"));return;}
                cursor+=consumed;remaining-=consumed;
                if(outputSize) {packet->data=output;packet->size=outputSize;decode();}
                if(!consumed && !outputSize)break;
            }
        }
        if(offset)buffer.remove(0,offset);
        present();
    }
    void decode() {
        int result=avcodec_send_packet(codec,packet);
        if(result<0 && result!=AVERROR(EAGAIN))return;
        while(avcodec_receive_frame(codec,frame)==0) {
            if(frame->width<1 || frame->height<1 || frame->width>16384 || frame->height>16384 || qint64(frame->width)*frame->height>40000000){fail(QCoreApplication::translate("NativeView","Frame resolution outside the limits"));return;}
            // Decode dependencies, but convert/upload only the newest picture
            // from this socket batch. This also applies to future iOS streams.
            av_frame_unref(pendingFrame);av_frame_move_ref(pendingFrame,frame);
        }
    }
    void present() {
            auto *frame=pendingFrame;if(!frame || !frame->data[0])return;
            scaler=sws_getCachedContext(scaler,frame->width,frame->height,AVPixelFormat(frame->format),frame->width,frame->height,AV_PIX_FMT_RGBA,SWS_FAST_BILINEAR,nullptr,nullptr,nullptr);
            if(!scaler){fail(QCoreApplication::translate("NativeView","The video frame could not be converted"));return;}
            // swscale's vectorized converters need aligned scanlines and tail
            // padding. QImage's tightly packed allocation is insufficient for
            // some widths (e.g. a 1080-pixel Android display with AVX-512).
            const int stride=(frame->width*4+63)&~63;
            QImage *slot=nullptr;
            for(auto &candidate:pool)if(candidate.isNull() || candidate.isDetached()){slot=&candidate;break;}
            // If all three images are still in use, retain the newest YUV frame
            // for the next batch instead of building an unbounded render queue.
            if(!slot){if(!presentScheduled){presentScheduled=true;QTimer::singleShot(4,this,[this]{presentScheduled=false;present();});}return;}
            if(slot->size()!=QSize(frame->width,frame->height)) {
                auto *pixels=static_cast<uchar *>(av_mallocz(qsizetype(stride)*(frame->height+1)+AV_INPUT_BUFFER_PADDING_SIZE));
                if(!pixels){fail(QCoreApplication::translate("NativeView","Not enough memory for the video frame"));return;}
                *slot=QImage(pixels,frame->width,frame->height,stride,QImage::Format_RGBA8888,[](void *p){av_free(p);},pixels);
            }
            auto &image=*slot;
            uint8_t *planes[]={image.bits(),nullptr,nullptr,nullptr};int strides[]={int(image.bytesPerLine()),0,0,0};
            sws_scale(scaler,frame->data,frame->linesize,0,frame->height,planes,strides);
            av_frame_unref(pendingFrame);
            bool notify=false;{QMutexLocker lock(&mutex);latest=image;if(!notified){notified=true;notify=true;}}
            if(notify)emit ready(epoch);
    }
    QLocalSocket *socket=nullptr;
    QByteArray buffer;
    AVCodecContext *codec=nullptr;
    AVCodecParserContext *parser=nullptr;
    AVFrame *frame=nullptr;
    AVFrame *pendingFrame=nullptr;
    AVPacket *packet=nullptr;
    SwsContext *scaler=nullptr;
    QMutex mutex;QImage latest;bool notified=false;quint64 epoch=0;
    bool jpeg=false,framed=false,presentScheduled=false;
    QByteArray prefix;
    QImage pool[3];
};

NativeView::NativeView(QQuickItem *parent):QQuickItem(parent),m_decoder(new VideoDecoder) {
    setFlag(ItemHasContents);setAcceptedMouseButtons(Qt::LeftButton);setActiveFocusOnTab(true);
    m_decoder->moveToThread(&m_thread);
    connect(&m_thread,&QThread::finished,m_decoder,&QObject::deleteLater);
    connect(m_decoder,&VideoDecoder::ready,this,[this](quint64 generation){
        if(generation!=m_generation)return;
        const auto previousSize=m_image.size();m_image=m_decoder->takeFrame();if(m_image.isNull())return;
        if(previousSize!=m_image.size()){emit sourceSizeChanged();emit contentRectChanged();}
        ++m_frames;m_dirty=true;if(m_state!="live"){m_state="live";m_error.clear();emit stateChanged();}emit frameChanged();update();
    });
    connect(m_decoder,&VideoDecoder::failed,this,[this](quint64 generation,const QString &error){if(generation!=m_generation)return;m_state="disconnected";m_error=error;emit stateChanged();});
    m_thread.start();
    m_statistics.setInterval(1000);connect(&m_statistics,&QTimer::timeout,this,[this]{m_fps=double(m_frames-m_previousFrames);m_previousFrames=m_frames;emit statisticsChanged();});m_statistics.start();
    m_wheelFrame.setSingleShot(true);m_wheelFrame.setInterval(16);
    connect(&m_wheelFrame,&QTimer::timeout,this,&NativeView::flushWheel);
    m_wheelEnd.setSingleShot(true);m_wheelEnd.setInterval(100);
    connect(&m_wheelEnd,&QTimer::timeout,this,&NativeView::finishWheel);
    connect(this,&QQuickItem::visibleChanged,this,[this]{if(!isVisible())finishWheel();});
    m_inputTimeout.setSingleShot(true);m_inputTimeout.setInterval(5000);
    connect(&m_inputTimeout,&QTimer::timeout,this,[this]{emit inputError(QCoreApplication::translate("NativeView","The input was not confirmed"));finishInput();});
    connect(&m_control,&QLocalSocket::connected,this,[this]{
        if(m_inputs.isEmpty())return;
        m_control.write(QJsonDocument(QJsonObject{{"operation","input"},{"arguments",m_inputs.head()}}).toJson(QJsonDocument::Compact)+'\n');
    });
    connect(&m_control,&QLocalSocket::readyRead,this,[this]{
        m_response+=m_control.readAll();if(m_response.size()>2*1024*1024){emit inputError(QCoreApplication::translate("NativeView","The runtime response is too large"));finishInput();return;}
        if(!m_response.contains('\n'))return;
        auto response=QJsonDocument::fromJson(m_response).object();
        if(!response.value("ok").toBool())emit inputError(response.value("error").toObject().value("message").toString(QCoreApplication::translate("NativeView","Input failed")));
        finishInput();
    });
    connect(&m_control,&QLocalSocket::errorOccurred,this,[this]{emit inputError(m_control.errorString());finishInput();});
}
NativeView::~NativeView() {QMetaObject::invokeMethod(m_decoder,[this]{m_decoder->stop();},Qt::BlockingQueuedConnection);m_thread.quit();m_thread.wait();}
void NativeView::setSession(const QVariantMap &session) {
    if(m_session==session)return;
    // Audio/status counters change independently of the display transport.
    // Keep the decoder, input queue and last frame alive for the same stream.
    bool sameStream=!session.value("session").toString().isEmpty();
    for(const auto *key:{"session","platform","status","video_socket","control_socket","codec","packetization","transport"})
        sameStream=sameStream && m_session.value(key)==session.value(key);
    if(sameStream){m_session=session;emit sessionChanged();return;}
    m_wheelFrame.stop();m_wheelEnd.stop();m_wheelActive=false;m_wheelDelta={};
    m_session=session;m_inputs.clear();m_inputTimeout.stop();{QSignalBlocker block(m_control);m_control.abort();}m_pressed=false;m_hasTouch=false;
    m_image={};m_frames=0;m_previousFrames=0;m_fps=0;m_dirty=true;m_error.clear();
    auto socket=session.value("video_socket").toString();m_state=socket.isEmpty()?"disconnected":"connecting";
    const auto generation=++m_generation;
    auto format=session.value("codec").toString();
    bool framed=session.value("packetization")=="access-unit" || session.value("transport").toString().startsWith("adb/scrcpy");
    QMetaObject::invokeMethod(m_decoder,[this,socket,generation,format,framed]{m_decoder->start(socket,generation,format,framed);},Qt::QueuedConnection);
    emit sessionChanged();emit stateChanged();emit frameChanged();emit sourceSizeChanged();emit contentRectChanged();update();
}
QRectF NativeView::contentRect() const {
    if(m_image.isNull())return {};
    QSizeF size=m_image.size();size.scale(boundingRect().size(),Qt::KeepAspectRatio);
    return QRectF((width()-size.width())/2,(height()-size.height())/2,size.width(),size.height());
}
QSGNode *NativeView::updatePaintNode(QSGNode *old,UpdatePaintNodeData *) {
    auto *node=static_cast<QSGSimpleTextureNode *>(old);
    if(m_image.isNull()){delete node;return nullptr;}
    if(!node){node=new QSGSimpleTextureNode;node->setOwnsTexture(true);m_dirty=true;}
    // setOwnsTexture(true) makes setTexture release the previous texture itself.
    if(m_dirty){node->setTexture(window()->createTextureFromImage(m_image));m_dirty=false;}
    node->setFiltering(QSGTexture::Linear);node->setRect(contentRect());return node;
}
void NativeView::geometryChange(const QRectF &now,const QRectF &before) {QQuickItem::geometryChange(now,before);emit contentRectChanged();update();}
void NativeView::sendInput(QVariantMap arguments) {
    if(m_session.value("session").toString().isEmpty())return;
    arguments["session"]=m_session.value("session");
    if(!m_image.isNull()){arguments["width"]=m_image.width();arguments["height"]=m_image.height();}
    auto request=QJsonObject::fromVariantMap(arguments);
    if(arguments.value("phase")=="move" && m_inputs.size()>1 && m_inputs.last().value("phase")=="move")m_inputs.last()=request;
    else if(m_inputs.size()<256)m_inputs.enqueue(request);
    else {emit inputError(QCoreApplication::translate("NativeView","Too many pending inputs"));return;}
    if(m_inputs.size()==1 && !m_inputTimeout.isActive())nextInput();
}
void NativeView::nextInput() {
    // A new input can arrive between finishing one request and the queued
    // call that starts the next; the request already under way stays alone.
    if(m_inputs.isEmpty() || m_control.state()!=QLocalSocket::UnconnectedState)return;
    m_response.clear();m_inputTimeout.start();m_control.connectToServer(m_session.value("control_socket").toString());
}
void NativeView::finishInput() {m_inputTimeout.stop();m_control.abort();m_response.clear();if(!m_inputs.isEmpty())m_inputs.dequeue();QTimer::singleShot(0,this,[this]{nextInput();});}
void NativeView::touch(const QPointF &point,const QString &phase) {
    auto rect=contentRect();if(rect.isEmpty())return;
    m_lastPoint=point;
    m_lastTarget={std::clamp((point.x()-rect.x())/rect.width(),0.0,1.0),std::clamp((point.y()-rect.y())/rect.height(),0.0,1.0)};m_hasTouch=true;
    sendInput({{"action","touch"},{"phase",phase},{"x",m_lastTarget.x()},{"y",m_lastTarget.y()}});
}
void NativeView::insertText(const QString &text) {
    if(!m_hasTouch){emit inputError(QCoreApplication::translate("NativeView","Click the text field in the app first"));return;}
    sendInput({{"action",m_session.value("platform")=="ios"?"fill":"text"},{"text",text},{"x",m_lastTarget.x()},{"y",m_lastTarget.y()}});
}
void NativeView::mousePressEvent(QMouseEvent *e) {if(!contentRect().contains(e->position())){e->ignore();return;}finishWheel();forceActiveFocus();m_pressed=true;touch(e->position(),"down");e->accept();}
void NativeView::mouseMoveEvent(QMouseEvent *e) {if(m_pressed)touch(e->position(),"move");e->accept();}
void NativeView::mouseReleaseEvent(QMouseEvent *e) {if(m_pressed)touch(e->position(),"up");m_pressed=false;e->accept();}
void NativeView::mouseUngrabEvent() {if(m_pressed)touch(m_lastPoint,"cancel");m_pressed=false;}
void NativeView::keyPressEvent(QKeyEvent *e) {
    static const QHash<int,QString> keys={{Qt::Key_Return,"enter"},{Qt::Key_Enter,"enter"},{Qt::Key_Backspace,"backspace"},{Qt::Key_Delete,"delete"},{Qt::Key_Escape,"escape"},{Qt::Key_Tab,"tab"},{Qt::Key_Left,"left"},{Qt::Key_Right,"right"},{Qt::Key_Up,"up"},{Qt::Key_Down,"down"},{Qt::Key_Home,"home"}};
    if(keys.contains(e->key()))sendInput({{"action","key"},{"key",keys[e->key()]}});
    else if(!e->text().isEmpty() && !(e->modifiers()&(Qt::ControlModifier|Qt::AltModifier|Qt::MetaModifier)))sendInput({{"action","text"},{"text",e->text()}});
    else {e->ignore();return;}e->accept();
}
void NativeView::wheelEvent(QWheelEvent *e) {
    const auto rect=contentRect();
    // Ctrl+wheel belongs to whatever surrounds the device, such as the zoom of
    // its own window.
    if(m_pressed || !rect.contains(e->position()) || e->modifiers().testFlag(Qt::ControlModifier)){e->ignore();return;}
    // One continuous gesture per wheel burst. Individual timed swipes blocked
    // the runtime for 160 ms each, accumulating seconds of delayed scrolling.
    const QPointF delta=!e->pixelDelta().isNull() ? QPointF(e->pixelDelta())
        : QPointF(e->angleDelta().x()/120.0*rect.width()*.09,e->angleDelta().y()/120.0*rect.height()*.09);
    if(!delta.isNull()) {
        if(!m_wheelActive){m_wheelPoint=e->position();m_wheelActive=true;touch(m_wheelPoint,"down");}
        m_wheelDelta+=delta;
        // Bound unsent movement as well as the existing coalesced touch queue.
        m_wheelDelta.setX(std::clamp(m_wheelDelta.x(),-rect.width()*.8,rect.width()*.8));
        m_wheelDelta.setY(std::clamp(m_wheelDelta.y(),-rect.height()*.8,rect.height()*.8));
        if(!m_wheelFrame.isActive())m_wheelFrame.start();
        m_wheelEnd.start();
    }
    if(e->phase()==Qt::ScrollEnd)finishWheel();
    e->accept();
}
void NativeView::flushWheel() {
    if(!m_wheelActive || m_wheelDelta.isNull())return;
    const auto rect=contentRect().adjusted(1,1,-1,-1);
    if(rect.isEmpty()){m_wheelDelta={};return;}
    const auto next=m_wheelPoint+m_wheelDelta;
    const QPointF bounded(std::clamp(next.x(),rect.left(),rect.right()),std::clamp(next.y(),rect.top(),rect.bottom()));
    if(bounded!=m_wheelPoint){m_wheelPoint=bounded;touch(m_wheelPoint,"move");}
    if(next!=bounded) {
        // Start another drag within the same scroll area when the finger reaches
        // an edge; no swipe sleeps, device reconnects or pending gesture backlog.
        touch(m_wheelPoint,"up");
        if(next.x()!=bounded.x())m_wheelPoint.setX(rect.center().x());
        if(next.y()!=bounded.y())m_wheelPoint.setY(rect.center().y());
        touch(m_wheelPoint,"down");
        // Consume the remaining distance immediately. Ending the burst here
        // must never leave a stationary down/up pair that clicks a control.
        const auto continued=m_wheelPoint+(next-bounded);
        m_wheelPoint={std::clamp(continued.x(),rect.left(),rect.right()),std::clamp(continued.y(),rect.top(),rect.bottom())};
        touch(m_wheelPoint,"move");
    }
    m_wheelDelta={};
}
void NativeView::finishWheel() {
    m_wheelFrame.stop();m_wheelEnd.stop();
    flushWheel();
    if(m_wheelActive)touch(m_wheelPoint,"up");
    m_wheelActive=false;m_wheelDelta={};
}
#include "nativeview.moc"
