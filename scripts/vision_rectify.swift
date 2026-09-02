// Apple Vision-based business-card rectifier.
//
// Strategy: try the newer VNDetectDocumentSegmentationRequest (macOS 12+),
// which is the document-specific ML model Apple recommended at WWDC21
// (session #10041) as the replacement for VNDetectRectanglesRequest.
// Its returned quadrilateral is oriented to the document's reading
// direction, so output is already landscape when the card is landscape —
// fixing the 90°-rotation bug we saw with VNDetectRectanglesRequest alone.
//
// Falls back to VNDetectRectanglesRequest when running on older macOS or
// when the segmentation request returns no quad.

import AppKit
import CoreImage
import Foundation
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

let args = CommandLine.arguments
guard args.count == 3 else {
    fail("usage: vision_rectify <input-image> <output-image>")
}

let inputURL = URL(fileURLWithPath: args[1])
let outputURL = URL(fileURLWithPath: args[2])

guard let image = CIImage(contentsOf: inputURL, options: [.applyOrientationProperty: true]) else {
    fail("failed to load input image")
}

func detectQuad() -> VNRectangleObservation? {
    let handler = VNImageRequestHandler(ciImage: image, options: [:])

    if #available(macOS 12.0, *) {
        let docRequest = VNDetectDocumentSegmentationRequest()
        if (try? handler.perform([docRequest])) != nil,
           let quad = docRequest.results?.first {
            return quad
        }
    }

    let rectRequest = VNDetectRectanglesRequest()
    rectRequest.maximumObservations = 1
    rectRequest.minimumConfidence = 0.45
    rectRequest.minimumAspectRatio = 0.25
    rectRequest.maximumAspectRatio = 1.0
    rectRequest.quadratureTolerance = 35
    guard (try? handler.perform([rectRequest])) != nil,
          let quad = rectRequest.results?.first else {
        return nil
    }
    return quad
}

guard let rectangle = detectQuad() else {
    fail("no rectangle detected")
}

let extent = image.extent
func vector(_ point: CGPoint) -> CIVector {
    CIVector(
        x: extent.origin.x + point.x * extent.width,
        y: extent.origin.y + point.y * extent.height
    )
}

guard let filter = CIFilter(name: "CIPerspectiveCorrection") else {
    fail("CIPerspectiveCorrection unavailable")
}
filter.setValue(image, forKey: kCIInputImageKey)
filter.setValue(vector(rectangle.topLeft), forKey: "inputTopLeft")
filter.setValue(vector(rectangle.topRight), forKey: "inputTopRight")
filter.setValue(vector(rectangle.bottomRight), forKey: "inputBottomRight")
filter.setValue(vector(rectangle.bottomLeft), forKey: "inputBottomLeft")

guard let corrected = filter.outputImage else {
    fail("perspective correction failed")
}

// Force landscape: if the corrected image came out portrait (taller than wide),
// rotate 90° clockwise so business-card aspect lands horizontal.
let correctedExtent = corrected.extent
let finalImage: CIImage = {
    if correctedExtent.height > correctedExtent.width {
        let rotation = CGAffineTransform(rotationAngle: -.pi / 2)
        return corrected.transformed(by: rotation)
    }
    return corrected
}()

let context = CIContext()
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
try FileManager.default.createDirectory(
    at: outputURL.deletingLastPathComponent(),
    withIntermediateDirectories: true
)
try context.writeJPEGRepresentation(
    of: finalImage,
    to: outputURL,
    colorSpace: colorSpace,
    options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.92]
)

print(outputURL.path)
