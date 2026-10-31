import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import "NativeProfiles.js" as NativeProfiles

// Static vector geometry surrounds the live texture. No extra video layer,
// framebuffer copy or synthetic status/navigation bar is involved.
// A profile with `body` and `ppi` is drawn to scale: its millimetres become
// points through the panel's density, so the body, bezels, buttons and
// cameras sit where they are on the device.
Item {
    id: frame
    property var profile: ({})
    property size sourceSize: Qt.size(0,0)
    property bool framed: true
    property int finishIndex: 0
    property bool unfolded: false
    // The device seen from behind: body, camera plateau and lenses in place
    // of the display, for a profile that describes its back.
    property bool rearView: false
    property color backgroundColor: "#14171e"
    // A detached device has no backdrop to hide the picture's square corners
    // behind, so the picture itself is cut to the screen's corner radius.
    property bool roundedScreen: false
    // Screen pixels per unit of this frame: its own scale, which a layer does
    // not see by itself, and the monitor's. The masked picture is rendered at
    // this size, or a zoomed window would show it enlarged from one texel a point.
    property real displayScale: scale
    readonly property real screenPixels: displayScale*Screen.devicePixelRatio
    readonly property size screenTextureSize: Qt.size(Math.ceil(screenHost.width*screenPixels),Math.ceil(screenHost.height*screenPixels))
    // Put the screen's edges on whole screen pixels (a live picture): a panel
    // pixel then lands on one screen pixel instead of between two.
    property bool pixelAligned: false
    // The picture itself carries the camera's cutout (see NativeProfiles).
    property bool pictureHasCutout: false
    // The software renderer cannot draw shader effects; it would show nothing
    // in place of the picture, so it keeps the square picture instead.
    readonly property bool maskScreen: roundedScreen && framed && cornerRadius>0 && GraphicsInfo.api!==GraphicsInfo.Software
    readonly property bool landscape: sourceSize.width>sourceSize.height
    // Size of the display in its own orientation, the one its pixels are
    // given in: upright for a phone, wide for the Duo's inner display.
    readonly property real logicalWidth: unfolded && profile.openWidth ? profile.openWidth : profile.width || 412
    readonly property real logicalHeight: unfolded && profile.openHeight ? profile.openHeight : profile.height || 892
    readonly property bool wide: logicalWidth>logicalHeight
    // The picture runs across the display's own orientation: the body turns.
    readonly property bool turned: sourceSize.width>0 && sourceSize.height>0 && landscape!==wide
    readonly property real portraitWidth: Math.min(logicalWidth,logicalHeight)
    // Preserve the actual stream's aspect ratio, even for rounded profile sizes
    // or when Android's resolution setting differs from the physical panel.
    readonly property real portraitHeight: sourceSize.width>0 && sourceSize.height>0 ? portraitWidth*Math.max(sourceSize.width,sourceSize.height)/Math.min(sourceSize.width,sourceSize.height) : Math.max(logicalWidth,logicalHeight)
    // The screen in the body's own coordinates (before turning).
    readonly property real screenWidth: wide ? portraitHeight : portraitWidth
    readonly property real screenHeight: wide ? portraitWidth : portraitHeight
    // Points per millimetre of this display: its points over its panel's
    // physical size (pixels over ppi). Zero without a pixel density.
    readonly property real ppi: unfolded && profile.openPpi ? profile.openPpi : profile.ppi || 0
    readonly property var panel: NativeProfiles.dimensions(profile,unfolded)
    readonly property real pointsPerMm: ppi>0 && panel[0]>0 ? portraitWidth/(Math.min(panel[0],panel[1])/ppi*25.4) : 0
    // The body's outline from the front, in millimetres (Apple's dimensions).
    readonly property var body: profile.body || ({})
    readonly property real bodyWidthMm: unfolded && body.openWidth ? body.openWidth : body.width || 0
    readonly property real bodyHeightMm: unfolded && body.openHeight ? body.openHeight : body.height || 0
    readonly property bool measured: pointsPerMm>0 && bodyWidthMm>0 && bodyHeightMm>0
    readonly property real profileBezel: profile.bezel===undefined?8:profile.bezel
    readonly property real bodyWidth: !framed ? screenWidth : measured ? Math.max(screenWidth+2,bodyWidthMm*pointsPerMm) : screenWidth+profileBezel*2
    readonly property real bodyHeight: !framed ? screenHeight : measured ? Math.max(screenHeight+2,bodyHeightMm*pointsPerMm) : screenHeight+profileBezel*2
    // Where the screen starts in the body: centred, unless the profile says
    // how far the active area is from the left and top edges (the Duo's
    // outer display sits further from its hinge).
    function offset(key, room) {const value=unfolded?body["open"+key[0].toUpperCase()+key.slice(1)]:body[key];return measured && value!==undefined?Math.min(room,value*pointsPerMm):room/2}
    function placed(value) {return pixelAligned ? NativeProfiles.snap(value+outerMargin,screenPixels)-outerMargin : value}
    readonly property real screenX: framed ? placed(offset("screenLeft",bodyWidth-screenWidth)) : 0
    readonly property real screenY: framed ? placed(offset("screenTop",bodyHeight-screenHeight)) : 0
    readonly property real bezel: Math.min(screenX,screenY,bodyWidth-screenWidth-screenX,bodyHeight-screenHeight-screenY)
    readonly property real outerMargin: framed ? 4 : 0
    // Apple's display and body corners are continuous curves, not arcs.
    readonly property bool continuous: profile.platform==="ios"
    readonly property var screenRadii: {
        const radii=unfolded?profile.openCornerRadii || (profile.openRadius!==undefined?null:profile.cornerRadii):profile.cornerRadii
        const one=(unfolded && profile.openRadius!==undefined ? profile.openRadius : profile.radius) || 0
        return framed ? (radii || [one,one,one,one]).map(r=>Math.min(r,portraitWidth/2)) : [0,0,0,0]
    }
    readonly property real cornerRadius: Math.max.apply(null,screenRadii)
    readonly property var bodyRadii: {
        const radii=unfolded?body.openRadii || (body.openWidth?null:body.radii):body.radii
        const fallback=cornerRadius+bezel
        const shown=measured && (radii || body.radius) ? (radii || [body.radius,body.radius,body.radius,body.radius]).map(r=>r*pointsPerMm) : [fallback,fallback,fallback,fallback]
        // Seen from behind, left and right change places.
        return rearShown ? [shown[1],shown[0],shown[3],shown[2]] : shown
    }
    readonly property real bodyRadius: Math.max.apply(null,bodyRadii)
    // The metal band seen from the front, outside the display's black border.
    readonly property real rail: measured && body.border!==undefined ? Math.max(1,bezel-body.border*pointsPerMm) : 2
    readonly property color finish: profile.colors && profile.colors.length ? profile.colors[Math.min(finishIndex,profile.colors.length-1)] : "#454b55"
    // A concentric line in the body, `inset` in from its edge.
    function bodyLine(inset) {return NativeProfiles.outline(inset,inset,bodyWidth-inset*2,bodyHeight-inset*2,bodyRadii.map(r=>Math.max(0,r-inset)),continuous)}
    // Polished metal seen from the front: light along the band, no dark patches.
    component Metal: LinearGradient {x1:0;y1:0;x2:frame.bodyWidth;y2:frame.bodyHeight
        GradientStop {position:0;color:Qt.lighter(frame.finish,1.3)} GradientStop {position:.28;color:frame.finish} GradientStop {position:.55;color:Qt.darker(frame.finish,1.12)} GradientStop {position:.8;color:frame.finish} GradientStop {position:1;color:Qt.lighter(frame.finish,1.2)}}
    readonly property string cutout: unfolded && profile.openCutout ? profile.openCutout : profile.cutout || "none"
    // The cutout's frame in points from the screen's top left, per state.
    function cutoutValue(key) {return unfolded?profile["open"+key[0].toUpperCase()+key.slice(1)]:profile[key]}
    readonly property bool punchCutout: cutout==="punch" || cutout==="punch-right"
    readonly property real cutoutWidth: cutoutValue("cutoutWidth") || (punchCutout?13:cutout==="notch"?164:119)
    readonly property real cutoutHeight: cutoutValue("cutoutHeight") || (punchCutout?13:cutout==="notch"?34:35)
    readonly property real cutoutY: cutoutValue("cutoutOffset")!==undefined ? cutoutValue("cutoutOffset") : cutout==="notch" ? 0 : 11
    readonly property real cutoutX: cutoutValue("cutoutX")!==undefined ? cutoutValue("cutoutX") : cutout==="punch-right" ? screenWidth-cutoutWidth-16 : (screenWidth-cutoutWidth)/2
    // Hardware buttons along the body's edges, as fractions of that edge
    // from its top (left, right) or its left end (top, bottom), seen from the
    // front. A profile without them keeps a side button and volume rocker.
    readonly property var buttons: (unfolded && profile.openButtons ? profile.openButtons : profile.buttons) || [{kind:"side",edge:"right",from:.22,length:48/Math.max(1,bodyHeight)},{kind:"volume",edge:"right",from:.34,length:76/Math.max(1,bodyHeight)}]
    readonly property var rear: (unfolded && profile.openBack ? profile.openBack : profile.back) || null
    readonly property bool rearShown: rearView && framed && !!rear && measured
    readonly property alias screen: screenHost
    readonly property rect screenRect: Qt.rect(screenHost.x,screenHost.y,screenHost.width,screenHost.height)
    signal hardwareKey(string key)
    // Bezel presses carry no display input; a detached window uses them to move.
    signal bezelPressed()
    // How far around the screen a press on the body counts as one on the
    // screen's edge, in this frame's units; zero keeps the body to itself.
    // Such a press and its drag are handed to the screen's owner, which
    // starts its touch at the edge: iOS opens its system gestures only there.
    property real screenReach: 0
    signal screenEdgePressed(point at)
    signal screenEdgeMoved(point at)
    signal screenEdgeReleased(point at)
    signal screenEdgeCanceled()
    width: (turned?bodyHeight:bodyWidth)+outerMargin*2
    height: (turned?bodyWidth:bodyHeight)+outerMargin*2
    // The screen's corners as shown: turned a quarter to the left, the top
    // right corner is the top left one.
    readonly property var shownScreenRadii: turned ? [screenRadii[1],screenRadii[2],screenRadii[3],screenRadii[0]] : screenRadii
    readonly property string screenPath: NativeProfiles.outline(0,0,screenHost.width,screenHost.height,shownScreenRadii,continuous)

    Item {
        id: shell
        anchors.centerIn:parent
        width:frame.bodyWidth;height:frame.bodyHeight
        rotation:frame.turned?-90:0
        visible:frame.framed
        Shape {
            anchors.fill:parent;preferredRendererType:Shape.CurveRenderer
            ShapePath {strokeWidth:1;strokeColor:Qt.lighter(frame.finish,1.1)
                fillGradient:Metal {}
                PathSvg {path:NativeProfiles.outline(0,0,shell.width,shell.height,frame.bodyRadii,frame.continuous)}
            }
        }
        // A folded device's hinge cover runs along its spine.
        Rectangle {
            objectName:"nativeHinge"
            readonly property string edge:frame.unfolded?"":frame.profile.foldEdge || ""
            visible:edge==="left" || edge==="right"
            readonly property bool leftSide:edge==="left"!==frame.rearShown
            x:leftSide?-2:parent.width-3;y:Math.max(frame.bodyRadius*.6,12);width:5;height:parent.height-y*2;radius:2
            color:Qt.darker(frame.finish,1.25);border.color:Qt.lighter(frame.finish,1.3);border.width:.5
        }
        Repeater {
            model:frame.framed?frame.buttons:[]
            Rectangle {
                id:button
                required property var modelData
                objectName:"nativeButton-"+modelData.kind
                // Seen from behind, left and right change places.
                readonly property string edge:frame.rearShown && (modelData.edge==="left" || modelData.edge==="right")?(modelData.edge==="left"?"right":"left"):modelData.edge
                readonly property bool across:edge==="top" || edge==="bottom"
                readonly property real along:across?shell.width:shell.height
                readonly property real start:(across && frame.rearShown?1-modelData.from-modelData.length:modelData.from)*along
                // Camera Control sits flush in the frame; the others stand out of it.
                readonly property bool flush:modelData.kind==="cameraControl"
                // The drawings' 0.45 mm proud of the band.
                readonly property real depth:flush?1.5:(frame.measured?.45*frame.pointsPerMm:3)+1
                x:across?start:edge==="left"?(flush?0:-depth+1):shell.width-(flush?1.5:1)
                y:across?(edge==="top"?(flush?0:-depth+1):shell.height-(flush?1.5:1)):start
                width:across?modelData.length*along:depth;height:across?depth:modelData.length*along
                radius:Math.min(width,height)/2
                color:flush?Qt.darker(frame.finish,1.6):frame.finish
                border.width:flush?.5:0;border.color:Qt.lighter(frame.finish,1.4)
                MouseArea {anchors.fill:parent
                    onClicked:mouse=>{
                        const kind=button.modelData.kind
                        if(kind==="side")frame.hardwareKey("power")
                        else if(kind==="volumeUp")frame.hardwareKey("volume_up")
                        else if(kind==="volumeDown")frame.hardwareKey("volume_down")
                        else if(kind==="volume")frame.hardwareKey((button.across?mouse.x<width/2:mouse.y<height/2)?"volume_up":"volume_down")
                    }
                }
            }
        }
    }
    // Dark backing so the antialiased edge of a masked picture never shows
    // the lighter body underneath.
    Shape {x:screenHost.x;y:screenHost.y;width:screenHost.width;height:screenHost.height;visible:frame.maskScreen && !frame.rearShown;preferredRendererType:Shape.CurveRenderer
        ShapePath {strokeWidth:0;fillColor:"#050609";PathSvg {path:frame.screenPath}}
    }
    Shape {id:screenMask;width:screenHost.width;height:screenHost.height;visible:false;layer.enabled:frame.maskScreen;preferredRendererType:Shape.CurveRenderer
        layer.textureSize:frame.screenTextureSize
        ShapePath {strokeWidth:0;fillColor:"black";PathSvg {path:frame.screenPath}}
    }
    Item {
        id:screenHost
        // A quarter turn to the left takes the screen's top edge to the left
        // and its right edge to the top.
        x:(frame.turned?frame.screenY:frame.screenX)+frame.outerMargin
        y:(frame.turned?frame.bodyWidth-frame.screenX-frame.screenWidth:frame.screenY)+frame.outerMargin
        width:frame.turned?frame.screenHeight:frame.screenWidth;height:frame.turned?frame.screenWidth:frame.screenHeight
        visible:!frame.rearShown
        layer.enabled:frame.maskScreen
        layer.textureSize:frame.screenTextureSize
        layer.effect:MultiEffect {maskEnabled:true;maskSource:screenMask;maskThresholdMin:.5;maskSpreadAtMin:1}
    }
    Item {
        id:overlay
        anchors.centerIn:parent;width:shell.width;height:shell.height;rotation:shell.rotation;visible:frame.framed
        Shape {
            id:rim;anchors.fill:parent;containsMode:Shape.FillContains;preferredRendererType:Shape.CurveRenderer
            ShapePath {strokeWidth:0;fillColor:frame.backgroundColor;fillRule:ShapePath.OddEvenFill
                PathSvg {path:NativeProfiles.outline(0,0,shell.width,shell.height,0,false)+" "+NativeProfiles.outline(0,0,shell.width,shell.height,frame.bodyRadii,frame.continuous)}
            }
            // The metal band again above the picture: an unmasked picture's
            // square corners reach under it where a continuous corner is tight.
            ShapePath {strokeWidth:0;fillRule:ShapePath.OddEvenFill
                fillGradient:Metal {}
                PathSvg {path:NativeProfiles.outline(.5,.5,shell.width-1,shell.height-1,frame.bodyRadii,frame.continuous)+" "+
                              NativeProfiles.outline(frame.rail,frame.rail,shell.width-frame.rail*2,shell.height-frame.rail*2,frame.bodyRadii.map(r=>Math.max(0,r-frame.rail)),frame.continuous)}
            }
            // The glass: black from inside the metal band to the screen.
            ShapePath {strokeWidth:0;fillColor:frame.rearShown?"transparent":"#050609";fillRule:ShapePath.OddEvenFill
                PathSvg {path:NativeProfiles.outline(frame.rail,frame.rail,shell.width-frame.rail*2,shell.height-frame.rail*2,frame.bodyRadii.map(r=>Math.max(0,r-frame.rail)),frame.continuous)+" "+
                              NativeProfiles.outline(frame.screenX,frame.screenY,frame.screenWidth,frame.screenHeight,frame.screenRadii,frame.continuous)}
            }
        }
        // The band's profile (the drawings' detail B): the edge falling away
        // outside, a highlight on its flat, and the slope down to the glass.
        Shape {
            anchors.fill:parent;visible:frame.measured;preferredRendererType:Shape.CurveRenderer
            ShapePath {strokeWidth:frame.rail*.125;strokeColor:Qt.darker(frame.finish,1.35);fillColor:"transparent"
                PathSvg {path:frame.bodyLine(frame.rail*.0625)}
            }
            ShapePath {strokeWidth:frame.rail*.09;strokeColor:Qt.lighter(frame.finish,1.45);fillColor:"transparent"
                PathSvg {path:frame.bodyLine(frame.rail*.3)}
            }
            ShapePath {strokeWidth:frame.rail*.25;strokeColor:Qt.darker(frame.finish,1.55);fillColor:"transparent"
                PathSvg {path:frame.bodyLine(frame.rail*.875)}
            }
        }
        // The back: glass in the case colour, the camera plateau and its parts
        // at their millimetre positions seen from behind (top left origin).
        Item {
            objectName:"nativeDeviceBack"
            visible:frame.rearShown
            anchors.fill:parent
            Shape {
                anchors.fill:parent;preferredRendererType:Shape.CurveRenderer
                ShapePath {strokeWidth:0
                    fillGradient:LinearGradient {x1:0;y1:0;x2:0;y2:shell.height
                        GradientStop {position:0;color:Qt.lighter(frame.finish,1.12)} GradientStop {position:1;color:Qt.darker(frame.finish,1.08)}}
                    PathSvg {path:NativeProfiles.outline(frame.rail,frame.rail,shell.width-frame.rail*2,shell.height-frame.rail*2,frame.bodyRadii.map(r=>Math.max(0,r-frame.rail)),frame.continuous)}
                }
            }
            Repeater {
                model:frame.rearShown?frame.rear.parts || []:[]
                Rectangle {
                    required property var modelData
                    readonly property string kind:modelData.kind
                    readonly property real mm:frame.pointsPerMm
                    x:modelData.x*mm;y:modelData.y*mm
                    width:modelData.width*mm;height:(modelData.height || modelData.width)*mm
                    // A part in the corner follows the body's corner.
                    readonly property real own:modelData.radius!==undefined?modelData.radius*mm:Math.min(width,height)/2
                    readonly property var radii:modelData.radii?modelData.radii.map(r=>r*mm):[own,own,own,own]
                    topLeftRadius:modelData.x<=0 && modelData.y<=0?Math.max(radii[0],frame.bodyRadii[0]-frame.rail):radii[0]
                    topRightRadius:modelData.x+modelData.width>=frame.bodyWidthMm-.01 && modelData.y<=0?Math.max(radii[1],frame.bodyRadii[1]-frame.rail):radii[1]
                    bottomRightRadius:radii[2];bottomLeftRadius:radii[3]
                    color:kind==="plateau"?Qt.lighter(frame.finish,1.06):kind==="flash"?"#e9e3d3":kind==="display"?"#050609":kind==="lens"?"#1b1d22":"#0d0e11"
                    border.width:kind==="plateau"?1:kind==="lens"?Math.max(1,width*.08):0
                    border.color:Qt.darker(frame.finish,kind==="plateau"?1.25:1.18)
                    // A lens: the dark glass and a reflection inside its ring.
                    Rectangle {visible:parent.kind==="lens";anchors.centerIn:parent;width:parent.width*.62;height:width;radius:width/2;color:"#07090e";border.color:"#1d2a40";border.width:1
                        Rectangle {x:parent.width*.22;y:parent.height*.18;width:parent.width*.22;height:width;radius:width/2;color:"#2c3e5c";opacity:.7}
                    }
                }
            }
        }
        // The inner display folds along its centre; the crease catches the light.
        Rectangle {
            objectName:"nativeCrease"
            visible:frame.unfolded && !frame.rearShown && !!frame.profile.openWidth
            x:frame.screenX+frame.screenWidth/2-width/2;y:frame.screenY;width:6;height:frame.screenHeight
            gradient:Gradient {orientation:Gradient.Horizontal
                GradientStop {position:0;color:"#00ffffff"} GradientStop {position:.42;color:"#10ffffff"} GradientStop {position:.5;color:"#1c000000"} GradientStop {position:.58;color:"#10ffffff"} GradientStop {position:1;color:"#00ffffff"}}
        }
        // Only bezel/corner hits are consumed. Real display coordinates pass
        // directly to NativeView, including drags that leave the screen.
        MouseArea {anchors.fill:parent;containmentMask:frame.rearShown?null:rim
            // This press is the screen's edge (see screenReach): it and its
            // drag go to the screen's owner, in the screen's coordinates.
            property bool edge:false
            function onScreen(mouse){return mapToItem(screenHost,mouse.x,mouse.y)}
            onPressed:mouse=>{
                const at=onScreen(mouse),reach=frame.rearShown?0:frame.screenReach
                edge=reach>0 && at.x>=-reach && at.y>=-reach && at.x<=screenHost.width+reach && at.y<=screenHost.height+reach
                if(edge)frame.screenEdgePressed(at);else frame.bezelPressed()
            }
            onPositionChanged:mouse=>{if(edge)frame.screenEdgeMoved(onScreen(mouse))}
            onReleased:mouse=>{if(edge){edge=false;frame.screenEdgeReleased(onScreen(mouse))}}
            onCanceled:{if(edge){edge=false;frame.screenEdgeCanceled()}}
        }
        // The camera's hole in the display: the island as a pill of its exact
        // size, a punch hole, or the TrueDepth notch reaching from the top edge.
        Item {
            id:camera;objectName:"nativeCameraCutout"
            visible:frame.cutout!=="none" && !frame.rearShown && !frame.pictureHasCutout
            readonly property bool punch:frame.punchCutout
            readonly property bool notch:frame.cutout==="notch"
            width:frame.cutoutWidth;height:frame.cutoutHeight
            x:frame.screenX+frame.cutoutX;y:frame.screenY+frame.cutoutY
            Rectangle {visible:!camera.notch;anchors.fill:parent;radius:Math.min(width,height)/2;color:"#020205"}
            // Edge-connected housing: concave shoulders into the top edge,
            // straight sides and rounded lower corners.
            Shape {
                id:notchShape
                visible:camera.notch;anchors.fill:parent;preferredRendererType:Shape.CurveRenderer
                readonly property real shoulder:frame.cutoutValue("cutoutShoulder")!==undefined?frame.cutoutValue("cutoutShoulder"):Math.min(6,width/10)
                readonly property real lower:Math.min(height,width/2,frame.cutoutValue("cutoutRadius")!==undefined?frame.cutoutValue("cutoutRadius"):Math.min(height*.6,width/6))
                ShapePath {strokeWidth:0;fillColor:"#020205"
                    startX:-notchShape.shoulder;startY:-1
                    PathLine {x:camera.width+notchShape.shoulder;y:-1}
                    PathArc {x:camera.width;y:notchShape.shoulder-1;radiusX:notchShape.shoulder;radiusY:notchShape.shoulder;direction:PathArc.Counterclockwise}
                    PathLine {x:camera.width;y:camera.height-notchShape.lower}
                    PathArc {x:camera.width-notchShape.lower;y:camera.height;radiusX:notchShape.lower;radiusY:notchShape.lower}
                    PathLine {x:notchShape.lower;y:camera.height}
                    PathArc {x:0;y:camera.height-notchShape.lower;radiusX:notchShape.lower;radiusY:notchShape.lower}
                    PathLine {x:0;y:notchShape.shoulder-1}
                    PathArc {x:-notchShape.shoulder;y:-1;radiusX:notchShape.shoulder;radiusY:notchShape.shoulder;direction:PathArc.Counterclockwise}
                }
            }
            // The front camera's lens: the right end of an island, the middle
            // of a punch hole, left of centre in a notch.
            Rectangle {
                readonly property real size:Math.min(camera.height*(camera.punch?.55:.32),camera.punch?22:9)
                width:size;height:size;radius:size/2;color:"#0b1322";border.color:"#172438";border.width:1
                anchors.verticalCenter:parent.verticalCenter
                x:camera.punch?(camera.width-size)/2:camera.notch?camera.width*.2:camera.width-camera.height/2-size/2
            }
            MouseArea {anchors.fill:parent}
        }
    }
}
