// Cuff monitor rig tray + stopcock clip - EXSU 620 Project 02, Group 2
// OpenSCAD (free, openscad.org): set `part`, press F6, then File > Export > Export as STL.
// PLA, 0.2 mm layers, 20% infill, no supports.
// Every value marked (M) is a starting value: measure it with calipers before printing.

part = "all";            // "tray", "clip" or "all"

// ---- rig tray: holds the half breadboard and the Pico on its Grove shield ----
bb    = [82.5, 54.5];    // (M) half breadboard footprint, mm
pico  = [56.0, 50.0];    // (M) Grove Shield for Pi Pico footprint, mm
hold  = 5;               // pocket depth, mm
wall  = 3; floor_t = 2; clr = 0.6; gap = 6;
usb_w = 14;              // notch for the USB cable, mm

// ---- stopcock clip: holds stopcocks A and B rigidly side by side ----
d      = 9.0;            // (M) stopcock body diameter where it is gripped, mm
pitch  = 40;             // (M) centre to centre, stopcock A to stopcock B, mm
clip_w = 12;             // clip width along the tube, mm
ring   = 2.4;            // clip wall thickness, mm
base_h = 3;              // clip base thickness, mm

$fn = 64;
L = wall*3 + bb[0] + pico[0] + gap;
W = wall*2 + max(bb[1], pico[1]);
H = floor_t + hold;

module tray(){
  difference(){
    cube([L, W, H]);
    translate([wall-clr/2, wall-clr/2, floor_t])             cube([bb[0]+clr, bb[1]+clr, hold+1]);     // breadboard pocket
    translate([wall*2+bb[0]+gap-clr/2, wall-clr/2, floor_t]) cube([pico[0]+clr, pico[1]+clr, hold+1]); // Pico shield pocket
    translate([L-wall-1, W/2-usb_w/2, floor_t])              cube([wall+2, usb_w, hold+1]);            // USB cable notch
    translate([wall+bb[0]-1, W/2-10, floor_t+1])             cube([gap+wall+2, 20, hold+1]);           // jumper wire pass
    translate([L/2, 1.2, H/2]) rotate([90,0,0]) linear_extrude(1.5)                                    // engraved reminder (FM-13)
      text("STOPCOCK B: MANOMETER OFF WHEN LOGGING", size=3.5, halign="center", valign="center");
  }
}

module c_ring(){
  difference(){
    cylinder(h=clip_w, r=d/2+ring);
    translate([0,0,-1]) cylinder(h=clip_w+2, r=d/2);
    translate([-0.375*d, 0, -1]) cube([0.75*d, d, clip_w+2]);  // snap-in opening
  }
}

module clip(){
  translate([-d/2-ring, -(d/2+ring)-base_h, 0]) cube([pitch+d+2*ring, base_h+0.5, clip_w]);
  for (x = [0, pitch]) translate([x, 0, 0]) c_ring();
}

if (part == "tray" || part == "all") tray();
if (part == "clip" || part == "all") translate([10, -15, 0]) clip();
