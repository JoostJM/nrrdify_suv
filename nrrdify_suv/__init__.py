#!/usr/bin/env python

# ========================================================================
#  Copyright Het Nederlands Kanker Instituut - Antoni van Leeuwenhoek
#
#  Licensed under the 3-clause BSD License
# ========================================================================

import datetime
import logging

import nrrdify
from nrrdify import commandline
import SimpleITK as sitk

import pydicom

pydicom.datadict.add_private_dict_entries('Philips PET Private Group', {  # {tag: (VR, VM, description) ...}
   0x70531000: ('DS', '1', 'SUV Scale Factor'), 
   0x70531009: ('DS', '1', 'Activity Concentration Scale Factor')
})


logger = logging.getLogger('nrrdify.suv')


def main(args=None):
  nrrdify.post_processing = post_processing
  commandline.main(args)


def post_processing(im, slices):
  global logger
  patient_name = str(getattr(slices[0], 'PatientName', '')).split('^')[0]
  study_date = getattr(slices[0], 'StudyDate', '19000101')
  series_description = getattr(slices[0], 'SeriesDescription', 'Unkn')
  series_number = getattr(slices[0], 'SeriesNumber', -1)

  radionuclide_sq = getattr(slices[0], 'RadiopharmaceuticalInformationSequence', None)
  if radionuclide_sq is None:
    logger.warning("Radionuclide information sequence not found in volume (patient %s, studydate %s series %d. %s), skipping...",
                   patient_name, study_date, series_number, series_description)
    return im  # probably not PET

  sanitychecks = {}
  sanity_keys = ('CorrectedImage', 'DecayCorrection', 'Units')
  for k in sanity_keys:
    sanitychecks[k] = getattr(slices[0], k, None)
    if sanitychecks[k] is None:
      logger.warning('Missing required sanity check tag "%s" in volume (patient %s, studydate %s series %d. %s), skipping...',
                     k, patient_name, study_date, series_number, series_description)
      return im

  if not ('ATTN' in sanitychecks['CorrectedImage'] and ('DECAY' in sanitychecks['CorrectedImage'] or 'DECY' in sanitychecks['CorrectedImage']) and
          (sanitychecks['DecayCorrection'] == 'START' or sanitychecks['DecayCorrection'] == 'ADMIN')):
    logger.warning('Sanity checks failed for volume (patient %s, studydate %s series %d. %s), skipping...',
                   patient_name, study_date, series_number, series_description)
    return im

  if 0x70531000 in slices[0]:
    if sanitychecks['Units'] == 'BQML':
      SUV_conversion_factor = slices[0][0x70531000].value
      CNT_conversion_factor = slices[0][0x70531009].value
      SUV_constant = SUV_conversion_factor / CNT_conversion_factor
      logger.info('Patient %s, studydate %s series %d. %s: Applying SUV conversion (SUV conversion constant %g, SUV conversion factor %g, parsed from (7053, 1000) / CNT conversion factor %g, parsed from (7053, 1009)',
                   patient_name, study_date, series_number, series_description, SUV_constant, SUV_conversion_factor, CNT_conversion_factor)
    elif sanitychecks['Units'] == 'CNTS':
      SUV_constant = slices[0][0x70531000].value
      logger.info('Patient %s, studydate %s series %d. %s: Applying SUV conversion (SUV conversion constant %g, parsed from (7053, 1000)',
                   patient_name, study_date, series_number, series_description, SUV_constant)
    else:
      logger.warning('Expecting unit to be BQML or CNTS, skipping...')
      return im
   
  else:
    if sanitychecks['Units'] != 'BQML':
      logger.warning('Expecting unit to be BQML, skipping...')
      return im
    required_tags = {}
    required_base_keys = ('SeriesTime', 'PatientWeight')
    required_seq_keys = ('RadionuclideHalfLife', 'RadionuclideTotalDose', 'RadiopharmaceuticalStartTime')

    for k in required_base_keys:
      required_tags[k] = getattr(slices[0], k, None)
      if required_tags[k] is None:
        logger.warning('Missing required tag "%s" in volume (patient %s, studydate %s series %d. %s), skipping...',
                       k, patient_name, study_date, series_number, series_description)
        return im
    for k in required_seq_keys:
      required_tags[k] = getattr(radionuclide_sq[0], k, None)
      if required_tags[k] is None:
        logger.warning('Missing required tag "%s" in volume (patient %s, studydate %s series %d. %s), skipping...',
                       k, patient_name, study_date, series_number, series_description)
        return im

    # Force cast to float
    injected_dose = float(required_tags['RadionuclideTotalDose'])
    bodyweight = float(required_tags['PatientWeight'])
    half_life = float(required_tags['RadionuclideHalfLife'])

    if sanitychecks['DecayCorrection'] == 'START':  # images are decay-corrected to image acquisition time (additional correction for interval administration-acquisition is needed)
      # Convert times to datetime and compute difference
      series_time = datetime.datetime.strptime(required_tags['SeriesTime'], '%H%M%S')
      admin_time = datetime.datetime.strptime(required_tags['RadiopharmaceuticalStartTime'], '%H%M%S')
      decay_time = (series_time - admin_time).total_seconds()

      # Compute total dose at acquisition start time
      decayed_dose = injected_dose * (2 ** (-decay_time / half_life))
    else:  # images are decay-corrected to administration time (so no additional correction needed)
      decayed_dose = injected_dose

    # Compute the SUV conversion factor
    SUV_constant = bodyweight * 1000 / decayed_dose

    logger.info('Patient %s, studydate %s series %d. %s: Applying SUV conversion (SUV conversion constant %g, injected dose (at acquisition start time) %g, body weight %g)',
                patient_name, study_date, series_number, series_description, SUV_constant, decayed_dose, bodyweight)

  im = sitk.Cast(im, sitk.sitkFloat32)
  im *= SUV_constant

  return im


if __name__ == '__main__':
  main()
