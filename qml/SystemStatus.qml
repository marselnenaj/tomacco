import QtQuick

Item {
    id: status
    property bool compact: false
    property bool android: false
    property color foreground: "#ffffff"
    property int battery: 100
    property int reception: 4
    implicitWidth: compact ? 36 : 72
    implicitHeight: compact ? 36 : 14
    onForegroundChanged: drawing.requestPaint()
    onBatteryChanged: drawing.requestPaint()
    onReceptionChanged: drawing.requestPaint()
    onCompactChanged: drawing.requestPaint()
    onAndroidChanged: drawing.requestPaint()
    Canvas {
        id: drawing
        anchors.fill: parent
        renderTarget: Canvas.FramebufferObject
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            let c=getContext("2d"); c.reset()
            c.strokeStyle=status.foreground; c.fillStyle=status.foreground
            c.lineCap="round"; c.lineJoin="round"
            function wifi(cx,cy,scale) {
                c.lineWidth=1.7*scale
                for(let r of [7,4.3]) {
                    c.beginPath(); c.arc(cx,cy,r*scale,Math.PI*1.23,Math.PI*1.77); c.stroke()
                }
                c.beginPath(); c.arc(cx,cy,1.05*scale,0,Math.PI*2); c.fill()
            }
            function rounded(x,y,w,h,r,fill) {
                c.beginPath(); c.moveTo(x+r,y); c.lineTo(x+w-r,y); c.quadraticCurveTo(x+w,y,x+w,y+r)
                c.lineTo(x+w,y+h-r); c.quadraticCurveTo(x+w,y+h,x+w-r,y+h)
                c.lineTo(x+r,y+h); c.quadraticCurveTo(x,y+h,x,y+h-r)
                c.lineTo(x,y+r); c.quadraticCurveTo(x,y,x+r,y)
                c.closePath(); if(fill)c.fill();else c.stroke()
            }
            if(status.compact) {
                c.scale(width/36,height/36)
                c.lineWidth=2.3
                c.globalAlpha=.3; c.beginPath(); c.arc(18,18,15,Math.PI*.75,Math.PI*2.25); c.stroke()
                c.globalAlpha=1; c.beginPath(); c.arc(18,18,15,Math.PI*.75,Math.PI*(.75+1.5*status.battery/100)); c.stroke()
                for(let i=0;i<5;i++) {
                    let a=Math.PI*(.29+i*.105)
                    c.globalAlpha=i<=status.reception?1:.3
                    c.beginPath(); c.arc(18+15*Math.cos(a),18+15*Math.sin(a),1.5,0,Math.PI*2); c.fill()
                }
                c.globalAlpha=1; wifi(18,23,1.05)
            } else if(status.android) {
                c.scale(width/72,height/14)
                c.beginPath(); c.moveTo(12,12); c.lineTo(3,3); c.quadraticCurveTo(12,-3,21,3); c.closePath(); c.fill()
                c.beginPath(); c.moveTo(30,12); c.lineTo(42,0); c.lineTo(42,12); c.closePath(); c.fill()
                rounded(56,2,8,11,1,true); rounded(58,0,4,2,.4,true)
                c.globalCompositeOperation="destination-out"
                if(status.battery<100) c.fillRect(57,3,6,9*(1-status.battery/100))
            } else {
                c.scale(width/72,height/14)
                for(let i=0;i<4;i++) { c.globalAlpha=i<status.reception?1:.3; rounded(i*4.5,10-i*2.5,3,3+i*2.5,.8,true) }
                c.globalAlpha=1; wifi(31,11,1)
                c.lineWidth=1; c.globalAlpha=.55; rounded(45,1,24,12,3,false)
                rounded(70,4.5,2,5,1,true); c.globalAlpha=1
                rounded(47,3,20*status.battery/100,8,1.5,true)
            }
        }
    }
}
