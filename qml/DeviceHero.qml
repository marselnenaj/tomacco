import QtQuick
import QtQuick.Layouts
import "."

// The stage before any device is on it, and so the first thing anyone sees: the
// tomato is the terminal. A huge red face under its green calyx, the sentence on
// it after a prompt, one action, and the three steps to a running app.
Item {
    id: hero
    property string title: ""
    property string body: ""
    property string actionText: ""
    property string secondaryText: ""
    property string errorText: ""
    // Each step: {label, detail, done, key}
    property var steps: []
    // Projects to open again at once, newest first: {name, path}.
    property var recent: []
    signal recentChosen(string path)
    signal action()
    signal secondary()
    signal stepChosen(string key)

    clip: true
    // The type follows the stage, never the tomato, so the text's height is known first.
    readonly property int margin: Theme.gapLg
    readonly property int titleSize: Math.max(Theme.fontDisplay,Math.min(84,Math.round(width*.066),Math.round(height*.1)))
    readonly property bool showBody: height>=460
    readonly property bool showSteps: height>=700 && width>=520
    readonly property real textHeight: column.implicitHeight

    // The tomato's half width, in the logo's 100 units, at a height in those units:
    // a lower bound read off the outline in the logo's path.
    function halfWidth(y) {
        const outline=[[22,25],[34,39],[46,44],[57,45],[64,44],[73,41],[86,31],[95,12],[97,0]]
        if(y<outline[0][0])return 0
        for(let i=1;i<outline.length;i++)if(y<=outline[i][0]) {
            const a=outline[i-1],b=outline[i]
            return a[1]+(b[1]-a[1])*(y-a[0])/(b[0]-a[0])
        }
        return 0
    }
    // Where the logo's square stands for a given size: with the calyx at the top, unless
    // the text needs more room below it; then the calyx slides out of the top edge.
    function topFor(size) {return Math.min(Theme.gapXs-size*.02,height-margin-textHeight-size*.46)}
    // The smallest tomato that still reaches past both edges at the bottom of the stage,
    // so no background shows in the lower corners.
    readonly property real side: {
        const w=Math.max(1,width),h=Math.max(1,height)
        let size=w*1.16
        for(let i=0;i<200;i++) {
            const bottom=(h-topFor(size))*100/size
            if(halfWidth(bottom)*size/100>=w*.52)break
            size*=1.02
        }
        return size
    }
    readonly property real ox: (width-side)/2
    readonly property real oy: topFor(side)
    // The text sits in the middle of what the calyx leaves free.
    readonly property real textTop: oy+side*.46
    readonly property real textWidth: Math.min(width-Theme.gapXl*2,Math.max(600,width*.74))

    Canvas {
        id: tomato
        anchors.fill: parent
        readonly property color red: Theme.accent
        readonly property color green: Theme.green
        readonly property real side: hero.side
        readonly property real offset: hero.oy
        onRedChanged: requestPaint()
        onGreenChanged: requestPaint()
        onSideChanged: requestPaint()
        onOffsetChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            const c=getContext("2d");c.reset()
            c.translate(hero.ox,hero.oy);c.scale(hero.side/100,hero.side/100)
            c.fillStyle=red
            c.beginPath();c.moveTo(50,25);c.bezierCurveTo(60,16,95,17,95,57);c.bezierCurveTo(95,81,74,97,50,97);c.bezierCurveTo(26,97,5,81,5,57);c.bezierCurveTo(5,17,40,16,50,25);c.closePath();c.fill()
            c.strokeStyle=green;c.lineWidth=9;c.lineCap="butt"
            for(const end of [[50,2],[71.87,17.89],[63.52,43.61],[36.48,43.61],[28.13,17.89]]) {c.beginPath();c.moveTo(50,25);c.lineTo(end[0],end[1]);c.stroke()}
        }
    }
    ColumnLayout {
        id: column
        objectName: "heroText"
        x: (hero.width-width)/2
        width: hero.textWidth
        y: Math.max(hero.textTop,hero.textTop+(hero.height-hero.margin-hero.textTop-height)/2)
        spacing: Theme.gapMd
        RowLayout {
            Layout.fillWidth: true
            spacing: hero.titleSize*.26
            // The prompt, as on the logo's face.
            Canvas {
                id: prompt
                Layout.alignment: Qt.AlignTop
                Layout.topMargin: hero.titleSize*.14
                Layout.preferredWidth: hero.titleSize*.58
                Layout.preferredHeight: hero.titleSize*.74
                readonly property color ink: Theme.ink
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                onPaint: {
                    const c=getContext("2d");c.reset()
                    c.strokeStyle=ink;c.lineWidth=width*.26;c.lineJoin="miter"
                    c.beginPath();c.moveTo(width*.1,height*.1);c.lineTo(width*.82,height*.5);c.lineTo(width*.1,height*.9);c.stroke()
                }
            }
            StudioHeading {
                objectName: "heroTitle"
                Layout.fillWidth: true
                text: hero.title
                color: Theme.ink
                size: hero.titleSize
            }
        }
        Text {
            Layout.fillWidth: true
            Layout.leftMargin: hero.titleSize*.84
            visible: hero.showBody
            text: hero.body
            color: Theme.ink
            font.pixelSize: Math.max(Theme.fontLead,Math.round(hero.titleSize*.3))
            font.weight: Font.Medium
            font.letterSpacing: -.3
            lineHeight: 1.1
            wrapMode: Text.WordWrap
        }
        Row {
            Layout.leftMargin: hero.titleSize*.84
            Layout.topMargin: Theme.gapXs
            spacing: Theme.gapXs
            StudioButton {
                objectName: "heroAction"
                text: hero.actionText
                iconName: "devices"
                inverted: true
                size: Theme.fontLead
                implicitHeight: Theme.gapXxxl
                leftPadding: Theme.gapLg
                rightPadding: Theme.gapLg
                onClicked: hero.action()
            }
            StudioButton {
                visible: !!hero.secondaryText
                text: hero.secondaryText
                inverted: true
                invertedFill: Theme.paper
                invertedText: Theme.ink
                size: Theme.fontLead
                implicitHeight: Theme.gapXxxl
                leftPadding: Theme.gapLg
                rightPadding: Theme.gapLg
                onClicked: hero.secondary()
            }
        }
        Flow {
            objectName: "heroRecent"
            Layout.fillWidth: true
            Layout.leftMargin: hero.titleSize*.84
            visible: hero.showBody && hero.recent.length>0
            spacing: Theme.gapXs
            Text {
                height: Theme.controlHeight
                verticalAlignment: Text.AlignVCenter
                rightPadding: Theme.gapXs
                text: qsTr("Recent")
                color: Theme.ink
                font.family: Theme.mono
                font.pixelSize: Theme.fontSmall
                font.weight: Font.Bold
            }
            Repeater {
                model: hero.recent.slice(0,3)
                StudioButton {
                    required property var modelData
                    required property int index
                    objectName: "heroRecent-"+index
                    text: modelData.name
                    iconName: "files"
                    tip: modelData.path
                    inverted: true
                    invertedFill: Theme.paper
                    invertedText: Theme.ink
                    onClicked: hero.recentChosen(modelData.path)
                }
            }
        }
        Text {
            Layout.fillWidth: true
            Layout.leftMargin: hero.titleSize*.84
            visible: !!hero.errorText
            text: hero.errorText
            color: Theme.ink
            font.family: Theme.mono
            font.pixelSize: Theme.fontSmall
            font.weight: Font.Bold
            wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: hero.titleSize*.84
            Layout.topMargin: Theme.gapMd
            spacing: Theme.gapMd
            visible: hero.showSteps
            Repeater {
                model: hero.steps
                Item {
                    id: step
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    implicitHeight: stepColumn.implicitHeight+Theme.gapSm
                    Rectangle {width:parent.width;height:3;color:Theme.ink;opacity:step.modelData.done?1:.35}
                    MouseArea {anchors.fill:parent;hoverEnabled:true;cursorShape:Qt.PointingHandCursor;onClicked:hero.stepChosen(step.modelData.key)}
                    Column {
                        id: stepColumn
                        y: Theme.gapSm
                        width: parent.width
                        spacing: Theme.gapXxs
                        StudioHeading {
                            width: parent.width
                            text: step.modelData.label
                            color: Theme.ink
                            size: Theme.fontTitle
                            wrapMode: Text.NoWrap
                            elide: Text.ElideRight
                        }
                        Text {
                            width: parent.width
                            text: step.modelData.detail
                            color: Theme.ink
                            font.family: Theme.mono
                            font.pixelSize: Theme.fontSmall
                            elide: Text.ElideMiddle
                        }
                    }
                }
            }
        }
    }
}
