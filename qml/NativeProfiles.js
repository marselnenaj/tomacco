.pragma library

function dimensions(profile, unfolded) {
    const pixels=String(unfolded?profile.openPixels || "":profile.pixels || "").match(/\d+/g)
    if(pixels && pixels.length===2)return pixels.map(Number)
    return [(unfolded?profile.openWidth:profile.width)*(profile.dpr || 1),(unfolded?profile.openHeight:profile.height)*(profile.dpr || 1)]
}
function variant(profile, width, height) {
    if(!width || !height)return {unfolded:false,compatible:true}
    const ratio=Math.min(width,height)/Math.max(width,height)
    let choices=[false];if(profile.openWidth)choices.push(true)
    for(let unfolded of choices) {
        const size=dimensions(profile,unfolded)
        if(Math.abs(Math.min(size[0],size[1])/Math.max(size[0],size[1])-ratio)<.003)return {unfolded:unfolded,compatible:true}
    }
    return {unfolded:false,compatible:false}
}
function match(profiles, platform, device, width, height) {
    const candidates=profiles.filter(p=>p.platform===platform)
    let assigned=candidates.find(p=>p.id===device.profile_id)
    if(assigned)return Object.assign({profile:assigned,source:device.profile_source || "assigned"},variant(assigned,width,height))
    // A native display's resolution alone does not prove a physical model.
    // Only emulators get the legacy, unambiguous display-profile fallback.
    if(device.kind!=="emulator" && !String(device.id || "").startsWith("emulator-"))return {profile:{},source:"unknown",compatible:false,unfolded:false}
    const matching=candidates.filter(p=>{let size=dimensions(p,false);return Math.min(size[0],size[1])===Math.min(width,height) && Math.max(size[0],size[1])===Math.max(width,height)})
    if(matching.length===1)return Object.assign({profile:matching[0],source:"display-match"},variant(matching[0],width,height))
    return {profile:{},source:"unknown",compatible:false,unfolded:false}
}
// A running guest of a model with an island draws that island itself, into its
// picture, at iOS's own size and place (372 x 106 pixels at 36 from the top on
// a 1206-pixel panel, whichever island the real model has). A second one from
// the profile would stick out around it, so the picture's is the only one.
// iOS draws no notch and no punch hole: those stay with the frame.
function pictureDrawsCutout(profile, cutout) {
    return !!profile.virtual && profile.virtual.islandBottom!==0 && (cutout==="island" || cutout==="compact")
}
// A length rounded to whole screen pixels, at `scale` screen pixels a unit.
function snap(value, scale) {
    return scale>0?Math.round(value*scale)/scale:value
}
// Where an item goes in its parent, near `wanted`, so that it starts on a whole
// physical pixel: a picture between pixels is resampled and goes soft.
function pixelOrigin(parentOrigin, wanted, devicePixelRatio) {
    return snap(parentOrigin+wanted,devicePixelRatio)-parentOrigin
}
// SVG path of a rounded rectangle: one radius, or four (top left, top
// right, bottom right, bottom left). Apple's corners are continuous: the
// curve starts 1.528665 radii from the corner and its curvature grows
// smoothly (the constants of UIKit's continuous corner curve); other
// corners are circular arcs.
function outline(x, y, w, h, radii, continuous) {
    const f=v=>Math.round(v*1000)/1000
    const span=continuous?1.52866483:1
    // A profile's own list comes through QML as a list type, not a JS array.
    const four=typeof radii==="number"?[radii,radii,radii,radii]:Array.from(radii || [0,0,0,0])
    const r=four.map(v=>Math.max(0,Math.min(v || 0,Math.min(w,h)/2/span)))
    const k=continuous?[[1.52866483,0],[1.08849296,0],[.86840694,0],[.63149399,.07491100],[.37282392,.16905899],[.16905899,.37282392],[.07491100,.63149399],[0,.86840694],[0,1.08849296],[0,1.52866483]]:null
    // One corner, from the edge that runs into it to the one that leaves it:
    // (ax,ay) points back along the first, (bx,by) along the second.
    const corner=(cx,cy,ax,ay,bx,by,radius)=>{
        const p=i=>f(cx+(ax*k[i][0]+bx*k[i][1])*radius)+" "+f(cy+(ay*k[i][0]+by*k[i][1])*radius)
        if(!(radius>0))return "L "+f(cx)+" "+f(cy)
        if(!continuous)return "L "+f(cx+ax*radius)+" "+f(cy+ay*radius)+" A "+f(radius)+" "+f(radius)+" 0 0 1 "+f(cx+bx*radius)+" "+f(cy+by*radius)
        return "L "+p(0)+" C "+p(1)+" "+p(2)+" "+p(3)+" C "+p(4)+" "+p(5)+" "+p(6)+" C "+p(7)+" "+p(8)+" "+p(9)
    }
    return ["M",f(x+w/2),f(y),corner(x+w,y,-1,0,0,1,r[1]),corner(x+w,y+h,0,-1,-1,0,r[2]),corner(x,y+h,1,0,0,-1,r[3]),corner(x,y,0,1,1,0,r[0]),"Z"].join(" ")
}
