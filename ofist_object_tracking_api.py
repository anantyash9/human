from threading import Thread

import cv2

from feature_extraction.rn50_api.resnet50_api import ResNet50ExtractorAPI
from feature_extraction.mars_api.mars_api import MarsExtractorAPI
from obj_tracking.ofist_api.tracker import Tracker
from tf_session.tf_session_utils import Pipe, Inference
import numpy as np


class OFISTObjectTrackingAPI:

    def __init__(self, max_age=10000, min_hits=5, flush_pipe_on_read=False):
        self.max_age = max_age
        self.min_hits = min_hits
        self.trackers = []
        self.frame_count = 0
        self.__bg_frame = None
        self.__bg_gray = None


        self.__flush_pipe_on_read = flush_pipe_on_read

        self.__feature_dim = (2048)
        self.__image_shape = (224, 224, 3)

        self.__thread = None
        self.__in_pipe = Pipe(self.__in_pipe_process)
        self.__out_pipe = Pipe(self.__out_pipe_process)

    number = 0

    def __extract_image_patch(self, image, bbox, patch_shape):

        sx, sy, ex, ey = np.array(bbox).astype(np.int)

        # dx = ex-sx
        # dx = int(.125*dx)
        #
        # dy = ey-sy
        # dy = int(.25*dy)
        dx = 0
        dy = 0

        image = image[sy+dy:ey-dy, sx+dx:ex-dx]
        image = cv2.resize(image, tuple(patch_shape[::-1]))
        return image

    def __in_pipe_process(self, inference):
        i_dets = inference.get_input()
        frame = i_dets.get_image()
        classes = i_dets.get_classes()
        boxes = i_dets.get_boxes_tlbr(normalized=False)
        masks = i_dets.get_masks()
        bboxes = []

        scores = i_dets.get_scores()
        for i in range(len(classes)):
            if classes[i] == i_dets.get_category('person') and scores[i] > .75:
                bboxes.append([boxes[i][1], boxes[i][0], boxes[i][3], boxes[i][2]])
        patches = []

        # if self.__bg_frame is None:
        #     self.__bg_frame = frame
        #     self.__bg_gray = cv2.cvtColor(self.__bg_frame, cv2.COLOR_BGR2GRAY)
        #     self.__bg_gray = cv2.GaussianBlur(self.__bg_gray, (5, 5), 0)
        #
        # gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # gray_frame = cv2.GaussianBlur(gray_frame, (5, 5), 0)
        # difference = cv2.absdiff(self.__bg_gray, gray_frame)
        # ret, mask = cv2.threshold(difference, 50, 255, cv2.THRESH_BINARY)
        # mask = np.stack((mask, mask, mask), axis=2)
        #
        # image = np.multiply(frame, mask)
        #
        # inference.get_meta_dict()['mask'] = mask
        # inference.get_meta_dict()['diff_img']=image

        image = frame
        # blur = cv2.GaussianBlur(image, (5, 5), 0)
        # image = cv2.addWeighted(image, 1.5, image, -0.5, 0)
        # image = cv2.cvtColor(frame, cv2.COLOR_BGR2HLS)
        for i in range(len(bboxes)):
            box = bboxes[i]
            # mask = masks[i]
            # mask = np.stack((mask, mask, mask), axis=2)
            # image = np.multiply(frame, mask)
            patch = self.__extract_image_patch(image, box, self.__image_shape[:2])
            if patch is None:
                print("WARNING: Failed to extract image patch: %s." % str(box))
                patch = np.random.uniform(0., 255., self.__image_shape).astype(np.uint8)
            patches.append(patch)

        inference.set_data(patches)
        inference.get_meta_dict()['bboxes'] = bboxes
        return inference

    def __out_pipe_process(self, inference):
        f_vecs = inference.get_result()

        # print(f_vecs.shape)
        inference = inference.get_meta_dict()['inference']
        bboxes = inference.get_meta_dict()['bboxes']
        self.frame_count = min(self.frame_count + 1, 1000)

        matched, unmatched_dets, unmatched_trks = Tracker.associate_detections_to_trackers(f_vecs, self.trackers, similarity_threshold=0.25)
        if bboxes:

            # update matched trackers with assigned detections
            for t, trk in enumerate(self.trackers):
                if (t not in unmatched_trks):
                    d = matched[np.where(matched[:, 1] == t)[0], 0][0]
                    trk.update(bboxes[d], f_vecs[d])  ## for dlib re-intialize the trackers ?!

            # create and initialise new trackers for unmatched detections
            for i in unmatched_dets:
                trk = Tracker(bboxes[i], f_vecs[i])
                self.trackers.append(trk)

        i = len(self.trackers)
        ret = []
        for trk in reversed(self.trackers):
            d = trk.get_bbox()
            if (trk.get_hit_streak() >= self.min_hits ): # or self.frame_count <= self.min_hits):
                ret.append(np.concatenate((d, [trk.get_id()])).reshape(1, -1))  # +1 as MOT benchmark requires positive
            i -= 1
            # remove dead tracklet
            if (trk.get_time_since_update() > self.max_age):
                self.trackers.pop(i)

        if (len(ret) > 0):
            inference.set_result(np.concatenate(ret))
        else:
            inference.set_result(np.empty((0, 5)))
        return inference

    def get_in_pipe(self):
        return self.__in_pipe

    def get_out_pipe(self):
        return self.__out_pipe

    def use_session_runner(self, session_runner):
        self.__session_runner = session_runner
        self.__encoder = MarsExtractorAPI(flush_pipe_on_read=True)
        self.__encoder.use_session_runner(session_runner)
        self.__enc_in_pipe = self.__encoder.get_in_pipe()
        self.__enc_out_pipe = self.__encoder.get_out_pipe()
        self.__encoder.run()

    def run(self):
        if self.__thread is None:
            self.__thread = Thread(target=self.__run)
            self.__thread.start()

    def __run(self):
        while self.__thread:

            if self.__in_pipe.is_closed():
                self.__out_pipe.close()
                return

            ret, inference = self.__in_pipe.pull(self.__flush_pipe_on_read)
            if ret:
                self.__job(inference)
            else:
                self.__in_pipe.wait()

    def __job(self, inference):
        self.__enc_in_pipe.push(
            Inference(inference.get_data(), meta_dict={'inference': inference}, return_pipe=self.__out_pipe))