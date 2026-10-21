"""Fictional prepared sources and explicit offline providers for Phase A3."""
import json
import re
import threading

from proposal_support import fixture
from src.providers.base import GenerationResponse

REFERENCE = 'doi:10.1234/fictional-protocol1'
BODY = ('Adults with condition delta received treatment alpha under the study protocol. '
        'The study team recorded medication use at scheduled clinic visits. '
        'Study staff retained the participant diaries for subsequent review. '
        'The protocol required documentation of the date and location of each assessment. '
        'The same questionnaire was used during scheduled visits. '
        'Concomitant medication use was recorded on the study forms. '
        'The clinical team reviewed the diary entries together with the visit assessments. '
        'The source records were retained by the study investigators. '
        'The study staff documented the clinical assessments on the designated forms. '
        'The diaries were collected at the study center during the scheduled clinic visits.')
GUIDELINE = ('For adults with condition delta, the panel suggests treatment alpha as a conditional '
             'recommendation with low certainty of evidence.')


def prepared(paragraphs=None, *, name='synthetic-secret-patient.pdf'):
    extraction, mapped, inventory = fixture(['Methods\n\n'+'\n\n'.join(paragraphs or [BODY])],name=name)
    extraction.update(reference_source_text=REFERENCE,reference_metadata={'doi':REFERENCE[4:],'reference':REFERENCE})
    return extraction,mapped,inventory


def response(request, text=None, **changes):
    header = json.loads(request.split('\n\n',1)[0])
    body = request.split(']\n',1)[1]
    entity = re.search(r'\btreatment [a-z]+',body,re.I)[0]
    common = {'topic':entity.title(),'subtopic':'Adults with condition delta','reference':header['verified_reference']}
    chunk = dict(common,section='Study population and protocol documentation',original_text=text or body)
    chunk.update(changes)
    return json.dumps({'article':dict(common,source_file=header['source_file'],number_of_chunks=1),'chunks':[chunk]})


class FakeProvider:
    generation_identity = {'provider':'offline-fixture','model':'fictional-v1'}

    def __init__(self, action=None):
        self.action = action
        self.calls = []
        self.active = self.high_water = 0
        self.lock = threading.Lock()

    def generate(self, request, alias, prompt):
        with self.lock:
            self.calls.append(request)
            self.active += 1
            self.high_water = max(self.high_water,self.active)
        try:
            raw = self.action(request,alias,prompt) if self.action else response(request)
            return GenerationResponse(raw,metadata={'usage':{'prompt_tokens':50,'completion_tokens':110}})
        finally:
            with self.lock:
                self.active -= 1


def execute(tmp_path, *, inputs=None, provider=None, **kwargs):
    from proposal_editorial import run_proposal_editorial
    return run_proposal_editorial(*(inputs or prepared()),provider=provider or FakeProvider(),
        workspace=kwargs.pop('workspace',tmp_path/'editorial'),db_path=tmp_path/'review.sqlite3',diagnostics={},**kwargs)
