import QtQuick
import QtQuick.Window
import "."

// Every icon of the interface: one stroke, square ends, on a 24-unit grid.
Canvas {
    id: icon
    property string name: ""
    property color color: Theme.muted
    implicitWidth: Theme.iconSize
    implicitHeight: Theme.iconSize
    // In the item's own units: Canvas applies the screen's scale itself, and a
    // canvas sized in device pixels would show only its top-left part.
    canvasSize: Qt.size(Math.max(1,Math.ceil(width)),Math.max(1,Math.ceil(height)))
    onNameChanged: requestPaint()
    onColorChanged: requestPaint()
    onCanvasSizeChanged: requestPaint()
    onPaint: {
        let c=getContext("2d");c.reset();c.scale(canvasSize.width/24,canvasSize.height/24)
        c.strokeStyle=color;c.fillStyle=color;c.lineWidth=1.8;c.lineCap="butt";c.lineJoin="miter"
        function line(points) {c.beginPath();c.moveTo(points[0],points[1]);for(let i=2;i<points.length;i+=2)c.lineTo(points[i],points[i+1]);c.stroke()}
        function box(x,y,w,h) {c.strokeRect(x,y,w,h)}
        function fill(x,y,w,h) {c.fillRect(x,y,w,h)}
        function circle(x,y,r) {c.beginPath();c.arc(x,y,r,0,Math.PI*2);c.stroke()}
        function arc(x,y,r,a,b) {c.beginPath();c.arc(x,y,r,a,b);c.stroke()}
        switch(name) {
        case "project": box(3,3,7,7);box(14,3,7,7);box(3,14,7,7);box(14,14,7,7);break
        case "phone": box(6,2,12,20);fill(10,18,4,1.8);break
        case "pin": line([8,3,16,3]);line([10,3,10,10,6,14,18,14,14,10,14,3]);line([12,14,12,21]);break
        case "edit": line([4,20,4,16,15,5,19,9,8,20,4,20]);line([12,8,16,12]);break
        case "record": c.beginPath();c.arc(12,12,6.5,0,Math.PI*2);c.fill();break
        case "grid": box(2,4,5,16);box(9.5,4,5,16);box(17,4,5,16);break
        case "qr": box(3,3,7,7);box(14,3,7,7);box(3,14,7,7);fill(14,14,3,3);fill(18,18,3,3);fill(5.5,5.5,2,2);fill(16.5,5.5,2,2);fill(5.5,16.5,2,2);break
        case "files": line([3,20,3,4,9.5,4,12,7,21,7,21,20,3,20]);break
        case "devices": box(2,4,13,12);line([5,20,12,20]);line([8.5,16,8.5,20]);box(17,9,5,12);break
        case "jobs": line([4,6,20,6]);line([4,12,20,12]);line([4,18,14,18]);fill(17,16,4,4);break
        case "agent": line([8,6,2,12,8,18]);line([16,6,22,12,16,18]);line([14,4,10,20]);break
        case "debug": box(7,8,10,13);line([12,8,12,21]);line([9,8,9,4]);line([15,8,15,4]);line([2,11,7,11]);line([17,11,22,11]);line([2,18,7,18]);line([17,18,22,18]);break
        case "testing": box(3,3,18,18);line([7,12,11,16,17,8]);break
        case "profiling": line([3,3,3,21,21,21]);line([7,16,11,10,15,13,21,5]);break
        case "release": line([12,16,12,3]);line([6,9,12,3,18,9]);line([3,15,3,21,21,21,21,15]);break
        case "search": box(4,4,12,12);line([15,15,21,21]);break
        case "play": c.beginPath();c.moveTo(7,4);c.lineTo(20,12);c.lineTo(7,20);c.closePath();c.fill();break
        case "chevron": line([6,9,12,15,18,9]);break
        case "chevronRight": line([9,6,15,12,9,18]);break
        case "back": line([15,5,8,12,15,19]);break
        case "close": line([5,5,19,19]);line([19,5,5,19]);break
        case "plus": line([12,4,12,20]);line([4,12,20,12]);break
        case "minus": line([4,12,20,12]);break
        case "check": line([4,12,10,18,20,6]);break
        case "refresh": line([20,3,20,9,14,9]);arc(12,12,8,-.1,Math.PI*1.65);break
        case "stop": fill(6,6,12,12);break
        case "pause": fill(6,5,4,14);fill(14,5,4,14);break
        case "power": line([12,2,12,11]);arc(12,13,8,-Math.PI/3,Math.PI*4/3);break
        case "volume": line([3,9,7,9,12,4,12,20,7,15,3,15,3,9]);arc(11,12,5,-.8,.8);arc(11,12,9,-.8,.8);break
        case "muted": line([3,9,7,9,12,4,12,20,7,15,3,15,3,9]);line([16,9,22,15]);line([22,9,16,15]);break
        case "web": box(3,3,18,18);line([3,9,21,9]);fill(6,5,2,2);fill(10,5,2,2);break
        case "copy": box(8,8,13,13);line([4,16,3,16,3,3,16,3,16,4]);break
        case "settings": line([3,7,13,7]);line([19,7,21,7]);box(13,4,6,6);line([3,17,5,17]);line([11,17,21,17]);box(5,14,6,6);break
        case "flip": box(3,4,8,16);line([14,8,21,8]);line([18,5,21,8,18,11]);line([21,16,14,16]);line([17,13,14,16,17,19]);break
        case "info": box(3,3,18,18);line([12,10.5,12,17]);fill(11,6.5,2,2);break
        case "home": line([3,11,12,3,21,11]);line([6,9,6,21,18,21,18,9]);break
        case "rotate": box(3,12,18,9);line([6,7,17,7]);line([14,3.5,17.5,7,14,10.5]);break
        case "camera": box(3,7,18,13);line([8,7,10,3.5,14,3.5,16,7]);circle(12,13.5,3.2);break
        case "keyboard": box(2,6,20,12);fill(5.5,9,2,2);fill(9.5,9,2,2);fill(13.5,9,2,2);fill(17,9,2,2);line([7,14.5,17,14.5]);break
        case "window": box(3,5,18,15);line([12,14,18,8]);line([13,8,18,8,18,13]);break
        case "dock": box(3,5,18,15);line([18,8,12,14]);line([12,9,12,14,17,14]);break
        case "frame": box(6,2,12,20);box(9,5,6,14);break
        case "color": box(3,3,18,18);fill(6,6,12,12);break
        case "activity": line([2,12,7,12,10,4,14,20,17,12,22,12]);break
        case "sun": box(8,8,8,8);line([12,2,12,5]);line([12,19,12,22]);line([2,12,5,12]);line([19,12,22,12]);line([5,5,7,7]);line([17,17,19,19]);line([19,5,17,7]);line([5,19,7,17]);break
        case "moon": c.beginPath();c.moveTo(20,14.5);c.bezierCurveTo(13,17,7,11,9.5,4);c.bezierCurveTo(3,6,2,14,6.5,18);c.bezierCurveTo(11,22,18,21,20,14.5);c.stroke();break
        case "terminal": box(2,4,20,16);line([6,9,10,12,6,15]);line([12,15,18,15]);break
        case "command": box(9,9,6,6);line([9,9,5,9,5,5,9,5,9,9]);line([15,9,19,9,19,5,15,5,15,9]);line([9,15,5,15,5,19,9,19,9,15]);line([15,15,19,15,19,19,15,19,15,15]);break
        case "fullscreen": line([3,9,3,3,9,3]);line([15,3,21,3,21,9]);line([21,15,21,21,15,21]);line([9,21,3,21,3,15]);break
        case "unplug": line([7,3,7,8]);line([13,3,13,8]);line([4,8,16,8,16,12,13,16,7,16,4,12,4,8]);line([10,16,10,21]);line([17,17,22,22]);line([22,17,17,22]);break
        case "external": box(3,6,15,15);line([11,13,21,3]);line([14,3,21,3,21,10]);break
        case "stepOver": line([3,14,3,10,12,5,21,10]);line([21,4,21,10,15,10]);fill(10,16,4,4);break
        case "stepIn": line([12,3,12,14]);line([7,9,12,14,17,9]);fill(10,18,4,4);break
        case "stepOut": line([12,14,12,3]);line([7,8,12,3,17,8]);fill(10,18,4,4);break
        }
    }
}
