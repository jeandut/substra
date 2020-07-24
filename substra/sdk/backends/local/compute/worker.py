# Copyright 2018 Owkin, inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
import json
import os
import pathlib
import shutil

from substra.sdk import schemas
from substra.runner import METRICS_FAKE_Y, METRICS_NO_FAKE_Y, DOCKER_METRICS_TAG
from substra.sdk.backends.local import dal
from substra.sdk.backends.local import models
from substra.sdk.backends.local import fs
from substra.sdk.backends.local.compute import spawner

_CONTAINER_MODEL_PATH = "/sandbox/model"

_VOLUME_INPUT_DATASAMPLES = {"bind": "/sandbox/data", "mode": "ro"}
_VOLUME_MODELS_RO = {"bind": _CONTAINER_MODEL_PATH, "mode": "ro"}
_VOLUME_MODELS_RW = {"bind": _CONTAINER_MODEL_PATH, "mode": "rw"}
_VOLUME_OPENER = {"bind": "/sandbox/opener/__init__.py", "mode": "ro"}
_VOLUME_OUTPUT_PRED = {"bind": "/sandbox/pred", "mode": "rw"}
_VOLUME_LOCAL = {"bind": "/sandbox/local", "mode": "rw"}
_VOLUME_LOCAL_READ_ONLY = {"bind": "/sandbox/local", "mode": "ro"}

_VOLUME_INPUT_MODELS_RO = {"bind": "/sandbox/input_models", "mode": "ro"}
_VOLUME_OUTPUT_MODELS_RW = {"bind": "/sandbox/output_models", "mode": "rw"}


def _mkdir(path, delete_if_exists=False):
    """Make directory (recursive)."""
    if os.path.exists(path):
        if not delete_if_exists:
            return path
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def _get_address_in_container(model_key, volume, container_volume):
    return pathlib.Path(os.path.join(volume, model_key).replace(volume, container_volume["bind"]))


class Worker:
    """ML Worker."""

    def __init__(self, db: dal.DataAccess):
        self._wdir = os.path.join(os.getcwd(), "local-worker")
        self._db = db
        self._spawner = spawner.get()

    @staticmethod
    def _is_local(key: str):
        """Check if the key corresponds to a local
        or remote asset.
        """
        return key.startswith("local_")

    def _get_data_volume(self, tuple_dir, tuple_):
        data_volume = _mkdir(os.path.join(tuple_dir, "data"))
        samples = [
            self._db.get(schemas.Type.DataSample, key) for key in tuple_.dataset.keys
        ]
        for sample in samples:
            # TODO more efficient link (symlink?)
            shutil.copytree(sample.path, os.path.join(data_volume, sample.pkhash))
        return data_volume

    def _save_output_model(self, tuple_, model_name, models_volume) -> models.OutModel:
        tmp_path = os.path.join(models_volume, model_name)
        model_dir = _mkdir(os.path.join(self._wdir, "models", tuple_.key))
        model_path = os.path.join(model_dir, model_name)
        shutil.copy(tmp_path, model_path)
        return models.OutModel(hash=fs.hash_file(model_path), storage_address=model_path)

    def _get_command_models_composite(self, is_train, tuple_, models_volume, container_volume):
        command = ""
        model_head_key = None
        model_trunk_key = None
        if not is_train:
            model_head_key = tuple_.out_head_model.out_model.hash_
            model_trunk_key = tuple_.out_trunk_model.out_model.hash_
        else:
            if tuple_.in_head_model:
                model_head_key = "input_head_model"

            if tuple_.in_trunk_model:
                model_trunk_key = "input_trunk_model"

        if model_head_key:
            head_model_container_address = _get_address_in_container(
                model_head_key,
                models_volume,
                container_volume
            )
            command += f" --input-head-model-filename {head_model_container_address}"

        if model_trunk_key:
            trunk_model_container_address = _get_address_in_container(
                model_trunk_key,
                models_volume,
                container_volume
            )
            command += f" --input-trunk-model-filename {trunk_model_container_address}"

        return command

    @contextlib.contextmanager
    def _context(self, key):
        try:
            tmp_dir = _mkdir(os.path.join(self._wdir, key))
            yield tmp_dir
        finally:
            # delete tuple working directory
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def schedule_traintuple(self, tuple_):
        """Schedules a ML task (blocking)."""
        with self._context(tuple_.key) as tuple_dir:
            tuple_.status = models.Status.doing

            # fetch dependencies
            algo = self._db.download(tuple_.algo_type, tuple_.algo.key)

            compute_plan = None
            if tuple_.compute_plan_id:
                compute_plan = self._db.get(schemas.Type.ComputePlan, tuple_.compute_plan_id)

            volumes = dict()
            # Prepare input models
            if isinstance(tuple_, models.CompositeTraintuple):
                input_models_volume = _mkdir(os.path.join(tuple_dir, "input_models"))
                output_models_volume = _mkdir(os.path.join(tuple_dir, "output_models"))
                if tuple_.in_head_model:
                    os.link(
                        tuple_.in_head_model.storage_address,
                        os.path.join(input_models_volume, "input_head_model")
                    )
                if tuple_.in_trunk_model:
                    os.link(
                        tuple_.in_trunk_model.storage_address,
                        os.path.join(input_models_volume, "input_trunk_model")
                    )

                volumes[input_models_volume] = _VOLUME_INPUT_MODELS_RO
                volumes[output_models_volume] = _VOLUME_OUTPUT_MODELS_RW

            elif tuple_.in_models is not None:
                # in models for traintuple and aggregatetuple
                models_volume = _mkdir(os.path.join(tuple_dir, "models"))
                for idx, model in enumerate(tuple_.in_models):
                    os.link(
                        model.storage_address,
                        os.path.join(models_volume, f"{idx}_{model.key}")
                    )

                volumes[models_volume] = _VOLUME_MODELS_RW

            if not isinstance(tuple_, models.Aggregatetuple):
                # if this is a traintuple or composite traintuple, prepare the data
                dataset = self._db.download(schemas.Type.Dataset, tuple_.dataset.key)
                volumes[dataset.opener.storage_address] = _VOLUME_OPENER
                if self._is_local(tuple_.dataset.key):
                    data_volume = self._get_data_volume(tuple_dir, tuple_)
                    volumes[data_volume] = _VOLUME_INPUT_DATASAMPLES

            if tuple_.compute_plan_id:
                #  Shared compute plan volume
                local_volume = _mkdir(
                    os.path.join(
                        self._wdir, "compute_plans", "local", tuple_.compute_plan_id
                    )
                )
                volumes[local_volume] = _VOLUME_LOCAL

            if isinstance(tuple_, models.Aggregatetuple):
                command = "aggregate"
            else:
                command = "train"

            # compute command
            command += f" --rank {tuple_.rank}"
            if not isinstance(tuple_, models.Aggregatetuple) \
                    and not self._is_local(tuple_.dataset.key):
                command += " --fake-data"
                command += f" --n-fake-samples {len(tuple_.dataset.keys)}"

            # Add the in_models to the command
            if isinstance(tuple_, models.CompositeTraintuple):
                command += self._get_command_models_composite(
                    is_train=True,
                    tuple_=tuple_,
                    models_volume=input_models_volume,
                    container_volume=_VOLUME_INPUT_MODELS_RO,
                )
            elif tuple_.in_models is not None:
                for idx, model in enumerate(tuple_.in_models):
                    command += f" {idx}_{model.key}"

            # Execute the tuple
            container_name = f"algo-{algo.key}"
            logs = self._spawner.spawn(
                container_name, str(algo.content.storage_address), command, volumes=volumes
            )

            # save move output models
            if isinstance(tuple_, models.CompositeTraintuple):
                tuple_.out_head_model.out_model = self._save_output_model(
                    tuple_,
                    'output_head_model',
                    output_models_volume
                )
                tuple_.out_trunk_model.out_model = self._save_output_model(
                    tuple_,
                    'output_trunk_model',
                    output_models_volume
                )
            else:
                tuple_.out_model = self._save_output_model(tuple_, 'model', models_volume)

            # set logs and status
            tuple_.log = logs
            tuple_.status = models.Status.done

            if tuple_.compute_plan_id:
                compute_plan = self._db.get(schemas.Type.ComputePlan, tuple_.compute_plan_id)
                compute_plan.done_count += 1
                if compute_plan.done_count == compute_plan.tuple_count:
                    compute_plan.status = models.Status.done

    def schedule_testtuple(self, tuple_):
        """Schedules a ML task (blocking)."""
        with self._context(tuple_.key) as tuple_dir:
            tuple_.status = models.Status.doing

            # fetch dependencies
            traintuple_type = schemas.Type(tuple_.traintuple_type)
            traintuple = self._db.get(traintuple_type, tuple_.traintuple_key)

            algo = self._db.download(traintuple.algo_type, traintuple.algo.key)
            objective = self._db.download(schemas.Type.Objective, tuple_.objective.key)
            dataset = self._db.download(schemas.Type.Dataset, tuple_.dataset.key)

            compute_plan = None
            if tuple_.compute_plan_id:
                compute_plan = self._db.get(schemas.Type.ComputePlan, tuple_.compute_plan_id)

            # prepare model and datasamples
            predictions_volume = _mkdir(os.path.join(tuple_dir, "pred"))
            models_volume = _mkdir(os.path.join(tuple_dir, "models"))

            volumes = {
                dataset.opener.storage_address: _VOLUME_OPENER,
                models_volume: _VOLUME_MODELS_RO,
                predictions_volume: _VOLUME_OUTPUT_PRED,
            }

            # If use fake data, no data volume
            if self._is_local(dataset.key):
                data_volume = self._get_data_volume(tuple_dir, tuple_)
                volumes[data_volume] = _VOLUME_INPUT_DATASAMPLES

            if tuple_.compute_plan_id:
                local_volume = _mkdir(
                    os.path.join(
                        self._wdir, "compute_plans", "local", tuple_.compute_plan_id
                    )
                )
                volumes[local_volume] = _VOLUME_LOCAL

            # compute testtuple command
            command = "predict"

            if not self._is_local(dataset.key):
                command += " --fake-data"
                command += f" --n-fake-samples {len(objective.test_dataset.data_sample_keys)}"

            if traintuple_type == schemas.Type.Traintuple \
                    or traintuple_type == schemas.Type.Aggregatetuple:
                os.link(
                    traintuple.out_model.storage_address,
                    os.path.join(models_volume, traintuple.out_model.key),
                )

                model_container_address = _get_address_in_container(
                    traintuple.out_model.key,
                    models_volume,
                    _VOLUME_MODELS_RW
                )
                command += f" {model_container_address}"
            elif traintuple_type == schemas.Type.CompositeTraintuple:
                os.link(
                    traintuple.out_head_model.out_model.storage_address,
                    os.path.join(models_volume, traintuple.out_head_model.out_model.hash_)
                )
                os.link(
                    traintuple.out_trunk_model.out_model.storage_address,
                    os.path.join(models_volume, traintuple.out_trunk_model.out_model.hash_)
                )
                command += self._get_command_models_composite(
                    is_train=False,
                    tuple_=traintuple,
                    models_volume=models_volume,
                    container_volume=_VOLUME_MODELS_RO,
                )

            container_name = f"algo-{traintuple.algo.key}"
            logs = self._spawner.spawn(
                container_name, str(algo.content.storage_address), command, volumes=volumes
            )

            # Calculate the metrics
            volumes = {
                predictions_volume: _VOLUME_OUTPUT_PRED,
                dataset.opener.storage_address: _VOLUME_OPENER,
            }

            if self._is_local(dataset.key):
                volumes[data_volume] = _VOLUME_INPUT_DATASAMPLES
                command = f"--fake-data-mode {METRICS_NO_FAKE_Y}"
            else:
                command = f"--fake-data-mode {METRICS_FAKE_Y}"
                command += f" --n-fake-samples {len(objective.test_dataset.data_sample_keys)}"

            container_name = DOCKER_METRICS_TAG
            logs_predict = self._spawner.spawn(
                container_name,
                str(objective.metrics.storage_address),
                command=command,
                volumes=volumes,
            )

            # save move performances
            tmp_path = os.path.join(predictions_volume, "perf.json")
            pred_dir = _mkdir(os.path.join(self._wdir, "performances", tuple_.key))
            pred_path = os.path.join(pred_dir, "performance.json")
            shutil.copy(tmp_path, pred_path)

            with open(pred_path, 'r') as f:
                tuple_.dataset.perf = json.load(f).get('all')

            # set logs and status
            tuple_.log = logs
            tuple_.log += "\n\n"
            tuple_.log += logs_predict
            tuple_.status = models.Status.done

            if compute_plan:
                compute_plan.done_count += 1
                if compute_plan.done_count == compute_plan.tuple_count:
                    compute_plan.status = models.Status.done