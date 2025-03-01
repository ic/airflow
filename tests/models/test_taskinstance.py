#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import datetime
import os
import signal
import time
import unittest
import urllib
from tempfile import NamedTemporaryFile
from typing import List, Optional, Union, cast
from unittest import mock
from unittest.mock import call, mock_open, patch

import pendulum
import pytest
from freezegun import freeze_time
from parameterized import param, parameterized
from sqlalchemy.orm.session import Session

from airflow import models, settings
from airflow.exceptions import (
    AirflowException,
    AirflowFailException,
    AirflowSensorTimeout,
    AirflowSkipException,
)
from airflow.models import (
    DAG,
    Connection,
    DagRun,
    Pool,
    RenderedTaskInstanceFields,
    TaskInstance as TI,
    TaskReschedule,
    Variable,
)
from airflow.models.taskinstance import load_error_file, set_error_file
from airflow.operators.bash import BashOperator
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import PythonOperator
from airflow.sensors.base import BaseSensorOperator
from airflow.sensors.python import PythonSensor
from airflow.serialization.serialized_objects import SerializedBaseOperator
from airflow.stats import Stats
from airflow.ti_deps.dependencies_deps import REQUEUEABLE_DEPS, RUNNING_DEPS
from airflow.ti_deps.dependencies_states import RUNNABLE_STATES
from airflow.ti_deps.deps.base_ti_dep import TIDepStatus
from airflow.ti_deps.deps.trigger_rule_dep import TriggerRuleDep
from airflow.utils import timezone
from airflow.utils.db import merge_conn
from airflow.utils.session import create_session, provide_session
from airflow.utils.state import State
from airflow.utils.types import DagRunType
from airflow.version import version
from tests.models import DEFAULT_DATE
from tests.test_utils import db
from tests.test_utils.asserts import assert_queries_count
from tests.test_utils.config import conf_vars
from tests.test_utils.db import clear_db_connections


class CallbackWrapper:
    task_id: Optional[str] = None
    dag_id: Optional[str] = None
    execution_date: Optional[datetime.datetime] = None
    task_state_in_callback: Optional[str] = None
    callback_ran = False

    def wrap_task_instance(self, ti):
        self.task_id = ti.task_id
        self.dag_id = ti.dag_id
        self.execution_date = ti.execution_date
        self.task_state_in_callback = ""
        self.callback_ran = False

    def success_handler(self, context):
        self.callback_ran = True
        session = settings.Session()
        temp_instance = (
            session.query(TI)
            .filter(TI.task_id == self.task_id)
            .filter(TI.dag_id == self.dag_id)
            .filter(TI.execution_date == self.execution_date)
            .one()
        )
        self.task_state_in_callback = temp_instance.state


class TestTaskInstance(unittest.TestCase):
    @staticmethod
    def clean_db():
        db.clear_db_dags()
        db.clear_db_pools()
        db.clear_db_runs()
        db.clear_db_task_fail()
        db.clear_rendered_ti_fields()
        db.clear_db_task_reschedule()

    def setUp(self):
        self.clean_db()
        with create_session() as session:
            test_pool = Pool(pool='test_pool', slots=1)
            session.add(test_pool)
            session.commit()

    def tearDown(self):
        self.clean_db()

    def test_load_error_file_returns_None_for_closed_file(self):
        error_fd = NamedTemporaryFile()
        error_fd.close()
        assert load_error_file(error_fd) is None

    def test_load_error_file_loads_correctly(self):
        error_message = "some random error message"
        with NamedTemporaryFile() as error_fd:
            set_error_file(error_fd.name, error=error_message)
            assert load_error_file(error_fd) == error_message

    def test_set_task_dates(self):
        """
        Test that tasks properly take start/end dates from DAGs
        """
        dag = DAG('dag', start_date=DEFAULT_DATE, end_date=DEFAULT_DATE + datetime.timedelta(days=10))

        op1 = DummyOperator(task_id='op_1', owner='test')

        assert op1.start_date is None and op1.end_date is None

        # dag should assign its dates to op1 because op1 has no dates
        dag.add_task(op1)
        assert op1.start_date == dag.start_date and op1.end_date == dag.end_date

        op2 = DummyOperator(
            task_id='op_2',
            owner='test',
            start_date=DEFAULT_DATE - datetime.timedelta(days=1),
            end_date=DEFAULT_DATE + datetime.timedelta(days=11),
        )

        # dag should assign its dates to op2 because they are more restrictive
        dag.add_task(op2)
        assert op2.start_date == dag.start_date and op2.end_date == dag.end_date

        op3 = DummyOperator(
            task_id='op_3',
            owner='test',
            start_date=DEFAULT_DATE + datetime.timedelta(days=1),
            end_date=DEFAULT_DATE + datetime.timedelta(days=9),
        )
        # op3 should keep its dates because they are more restrictive
        dag.add_task(op3)
        assert op3.start_date == DEFAULT_DATE + datetime.timedelta(days=1)
        assert op3.end_date == DEFAULT_DATE + datetime.timedelta(days=9)

    def test_timezone_awareness(self):
        naive_datetime = DEFAULT_DATE.replace(tzinfo=None)

        # check ti without dag (just for bw compat)
        op_no_dag = DummyOperator(task_id='op_no_dag')
        ti = TI(task=op_no_dag, execution_date=naive_datetime)

        assert ti.execution_date == DEFAULT_DATE

        # check with dag without localized execution_date
        dag = DAG('dag', start_date=DEFAULT_DATE)
        op1 = DummyOperator(task_id='op_1')
        dag.add_task(op1)
        ti = TI(task=op1, execution_date=naive_datetime)

        assert ti.execution_date == DEFAULT_DATE

        # with dag and localized execution_date
        tzinfo = pendulum.timezone("Europe/Amsterdam")
        execution_date = timezone.datetime(2016, 1, 1, 1, 0, 0, tzinfo=tzinfo)
        utc_date = timezone.convert_to_utc(execution_date)
        ti = TI(task=op1, execution_date=execution_date)
        assert ti.execution_date == utc_date

    def test_task_naive_datetime(self):
        naive_datetime = DEFAULT_DATE.replace(tzinfo=None)

        op_no_dag = DummyOperator(
            task_id='test_task_naive_datetime', start_date=naive_datetime, end_date=naive_datetime
        )

        assert op_no_dag.start_date.tzinfo
        assert op_no_dag.end_date.tzinfo

    def test_set_dag(self):
        """
        Test assigning Operators to Dags, including deferred assignment
        """
        dag = DAG('dag', start_date=DEFAULT_DATE)
        dag2 = DAG('dag2', start_date=DEFAULT_DATE)
        op = DummyOperator(task_id='op_1', owner='test')

        # no dag assigned
        assert not op.has_dag()
        with pytest.raises(AirflowException):
            getattr(op, 'dag')

        # no improper assignment
        with pytest.raises(TypeError):
            op.dag = 1

        op.dag = dag

        # no reassignment
        with pytest.raises(AirflowException):
            op.dag = dag2

        # but assigning the same dag is ok
        op.dag = dag

        assert op.dag is dag
        assert op in dag.tasks

    def test_infer_dag(self):
        dag = DAG('dag', start_date=DEFAULT_DATE)
        dag2 = DAG('dag2', start_date=DEFAULT_DATE)

        op1 = DummyOperator(task_id='test_op_1', owner='test')
        op2 = DummyOperator(task_id='test_op_2', owner='test')
        op3 = DummyOperator(task_id='test_op_3', owner='test', dag=dag)
        op4 = DummyOperator(task_id='test_op_4', owner='test', dag=dag2)

        # double check dags
        assert [i.has_dag() for i in [op1, op2, op3, op4]] == [False, False, True, True]

        # can't combine operators with no dags
        with pytest.raises(AirflowException):
            op1.set_downstream(op2)

        # op2 should infer dag from op1
        op1.dag = dag
        op1.set_downstream(op2)
        assert op2.dag is dag

        # can't assign across multiple DAGs
        with pytest.raises(AirflowException):
            op1.set_downstream(op4)
        with pytest.raises(AirflowException):
            op1.set_downstream([op3, op4])

    def test_bitshift_compose_operators(self):
        dag = DAG('dag', start_date=DEFAULT_DATE)
        with dag:
            op1 = DummyOperator(task_id='test_op_1', owner='test')
            op2 = DummyOperator(task_id='test_op_2', owner='test')
            op3 = DummyOperator(task_id='test_op_3', owner='test')

            op1 >> op2 << op3

        # op2 should be downstream of both
        assert op2 in op1.downstream_list
        assert op2 in op3.downstream_list

    @patch.object(DAG, 'get_concurrency_reached')
    def test_requeue_over_dag_concurrency(self, mock_concurrency_reached):
        mock_concurrency_reached.return_value = True

        dag = DAG(
            dag_id='test_requeue_over_dag_concurrency',
            start_date=DEFAULT_DATE,
            max_active_runs=1,
            max_active_tasks=2,
        )
        task = DummyOperator(task_id='test_requeue_over_dag_concurrency_op', dag=dag)

        ti = TI(task=task, execution_date=timezone.utcnow(), state=State.QUEUED)
        # TI.run() will sync from DB before validating deps.
        with create_session() as session:
            session.add(ti)
            session.commit()
        ti.run()
        assert ti.state == State.NONE

    def test_requeue_over_task_concurrency(self):
        dag = DAG(
            dag_id='test_requeue_over_task_concurrency',
            start_date=DEFAULT_DATE,
            max_active_runs=1,
            max_active_tasks=2,
        )
        task = DummyOperator(task_id='test_requeue_over_task_concurrency_op', dag=dag, task_concurrency=0)

        ti = TI(task=task, execution_date=timezone.utcnow(), state=State.QUEUED)
        # TI.run() will sync from DB before validating deps.
        with create_session() as session:
            session.add(ti)
            session.commit()
        ti.run()
        assert ti.state == State.NONE

    def test_requeue_over_pool_concurrency(self):
        dag = DAG(
            dag_id='test_requeue_over_pool_concurrency',
            start_date=DEFAULT_DATE,
            max_active_runs=1,
            max_active_tasks=2,
        )
        task = DummyOperator(task_id='test_requeue_over_pool_concurrency_op', dag=dag, task_concurrency=0)

        ti = TI(task=task, execution_date=timezone.utcnow(), state=State.QUEUED)
        # TI.run() will sync from DB before validating deps.
        with create_session() as session:
            pool = session.query(Pool).filter(Pool.pool == 'test_pool').one()
            pool.slots = 0
            session.add(ti)
            session.commit()
        ti.run()
        assert ti.state == State.NONE

    def test_not_requeue_non_requeueable_task_instance(self):
        dag = models.DAG(dag_id='test_not_requeue_non_requeueable_task_instance')
        # Use BaseSensorOperator because sensor got
        # one additional DEP in BaseSensorOperator().deps
        task = BaseSensorOperator(
            task_id='test_not_requeue_non_requeueable_task_instance_op',
            dag=dag,
            pool='test_pool',
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=timezone.utcnow(), state=State.QUEUED)
        with create_session() as session:
            session.add(ti)
            session.commit()

        all_deps = RUNNING_DEPS | task.deps
        all_non_requeueable_deps = all_deps - REQUEUEABLE_DEPS
        patch_dict = {}
        for dep in all_non_requeueable_deps:
            class_name = dep.__class__.__name__
            dep_patch = patch(f'{dep.__module__}.{class_name}.{dep._get_dep_statuses.__name__}')
            method_patch = dep_patch.start()
            method_patch.return_value = iter([TIDepStatus('mock_' + class_name, True, 'mock')])
            patch_dict[class_name] = (dep_patch, method_patch)

        for class_name, (dep_patch, method_patch) in patch_dict.items():
            method_patch.return_value = iter([TIDepStatus('mock_' + class_name, False, 'mock')])
            ti.run()
            assert ti.state == State.QUEUED
            dep_patch.return_value = TIDepStatus('mock_' + class_name, True, 'mock')

        for (dep_patch, method_patch) in patch_dict.values():
            dep_patch.stop()

    def test_mark_non_runnable_task_as_success(self):
        """
        test that running task with mark_success param update task state
        as SUCCESS without running task despite it fails dependency checks.
        """
        non_runnable_state = (set(State.task_states) - RUNNABLE_STATES - set(State.SUCCESS)).pop()
        dag = models.DAG(dag_id='test_mark_non_runnable_task_as_success')
        task = DummyOperator(
            task_id='test_mark_non_runnable_task_as_success_op',
            dag=dag,
            pool='test_pool',
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=timezone.utcnow(), state=non_runnable_state)
        # TI.run() will sync from DB before validating deps.
        with create_session() as session:
            session.add(ti)

            dag.create_dagrun(
                execution_date=ti.execution_date,
                state=State.RUNNING,
                run_type=DagRunType.SCHEDULED,
                session=session,
            )
            session.commit()
        ti.run(mark_success=True)
        assert ti.state == State.SUCCESS

    def test_run_pooling_task(self):
        """
        test that running a task in an existing pool update task state as SUCCESS.
        """
        dag = models.DAG(dag_id='test_run_pooling_task')
        task = DummyOperator(
            task_id='test_run_pooling_task_op',
            dag=dag,
            pool='test_pool',
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=timezone.utcnow())

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )
        ti.run()

        db.clear_db_pools()
        assert ti.state == State.SUCCESS

    def test_pool_slots_property(self):
        """
        test that try to create a task with pool_slots less than 1
        """

        def create_task_instance():
            dag = models.DAG(dag_id='test_run_pooling_task')
            task = DummyOperator(
                task_id='test_run_pooling_task_op',
                dag=dag,
                pool='test_pool',
                pool_slots=0,
                owner='airflow',
                start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
            )
            return TI(task=task, execution_date=timezone.utcnow())

        with pytest.raises(AirflowException):
            create_task_instance()

    @provide_session
    def test_ti_updates_with_task(self, session=None):
        """
        test that updating the executor_config propagates to the TaskInstance DB
        """
        with models.DAG(dag_id='test_run_pooling_task') as dag:
            task = DummyOperator(
                task_id='test_run_pooling_task_op',
                owner='airflow',
                executor_config={'foo': 'bar'},
                start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
            )
        ti = TI(task=task, execution_date=timezone.utcnow())

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
            session=session,
        )

        ti.run(session=session)
        tis = dag.get_task_instances()
        assert {'foo': 'bar'} == tis[0].executor_config
        with models.DAG(dag_id='test_run_pooling_task') as dag:
            task2 = DummyOperator(
                task_id='test_run_pooling_task_op',
                owner='airflow',
                executor_config={'bar': 'baz'},
                start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
            )

        ti = TI(task=task2, execution_date=timezone.utcnow())

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
            session=session,
        )
        ti.run(session=session)
        tis = dag.get_task_instances()
        assert {'bar': 'baz'} == tis[1].executor_config
        session.rollback()

    def test_run_pooling_task_with_mark_success(self):
        """
        test that running task in an existing pool with mark_success param
        update task state as SUCCESS without running task
        despite it fails dependency checks.
        """
        dag = models.DAG(dag_id='test_run_pooling_task_with_mark_success')
        task = DummyOperator(
            task_id='test_run_pooling_task_with_mark_success_op',
            dag=dag,
            pool='test_pool',
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=timezone.utcnow())

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )
        ti.run(mark_success=True)
        assert ti.state == State.SUCCESS

    def test_run_pooling_task_with_skip(self):
        """
        test that running task which returns AirflowSkipOperator will end
        up in a SKIPPED state.
        """

        def raise_skip_exception():
            raise AirflowSkipException

        dag = models.DAG(dag_id='test_run_pooling_task_with_skip')
        task = PythonOperator(
            task_id='test_run_pooling_task_with_skip',
            dag=dag,
            python_callable=raise_skip_exception,
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=timezone.utcnow())
        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )
        ti.run()
        assert State.SKIPPED == ti.state

    def test_task_sigterm_works_with_retries(self):
        """
        Test that ensures that tasks are retried when they receive sigterm
        """
        dag = DAG(dag_id='test_mark_failure_2', start_date=DEFAULT_DATE, default_args={'owner': 'owner1'})

        def task_function(ti):
            # pylint: disable=unused-argument
            os.kill(ti.pid, signal.SIGTERM)

        task = PythonOperator(
            task_id='test_on_failure',
            python_callable=task_function,
            retries=1,
            retry_delay=datetime.timedelta(seconds=2),
            dag=dag,
        )

        dag.create_dagrun(
            run_id="test",
            state=State.RUNNING,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
        )
        ti = TI(task=task, execution_date=DEFAULT_DATE)
        ti.refresh_from_db()
        with self.assertRaises(AirflowException):
            ti.run()
        ti.refresh_from_db()
        assert ti.state == State.UP_FOR_RETRY

    def test_retry_delay(self):
        """
        Test that retry delays are respected
        """
        dag = models.DAG(dag_id='test_retry_handling')
        task = BashOperator(
            task_id='test_retry_handling_op',
            bash_command='exit 1',
            retries=1,
            retry_delay=datetime.timedelta(seconds=3),
            dag=dag,
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )

        def run_with_error(ti):
            try:
                ti.run()
            except AirflowException:
                pass

        ti = TI(task=task, execution_date=timezone.utcnow())
        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )

        assert ti.try_number == 1
        # first run -- up for retry
        run_with_error(ti)
        assert ti.state == State.UP_FOR_RETRY
        assert ti.try_number == 2

        # second run -- still up for retry because retry_delay hasn't expired
        run_with_error(ti)
        assert ti.state == State.UP_FOR_RETRY

        # third run -- failed
        time.sleep(3)
        run_with_error(ti)
        assert ti.state == State.FAILED

    def test_retry_handling(self):
        """
        Test that task retries are handled properly
        """
        expected_rendered_ti_fields = {'env': None, 'bash_command': 'echo test_retry_handling; exit 1'}

        dag = models.DAG(dag_id='test_retry_handling')
        task = BashOperator(
            task_id='test_retry_handling_op',
            bash_command='echo {{dag.dag_id}}; exit 1',
            retries=1,
            retry_delay=datetime.timedelta(seconds=0),
            dag=dag,
            owner='test_pool',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )

        def run_with_error(ti):
            try:
                ti.run()
            except AirflowException:
                pass

        ti = TI(task=task, execution_date=timezone.utcnow())
        assert ti.try_number == 1

        # first run -- up for retry
        run_with_error(ti)
        assert ti.state == State.UP_FOR_RETRY
        assert ti._try_number == 1
        assert ti.try_number == 2

        # second run -- fail
        run_with_error(ti)
        assert ti.state == State.FAILED
        assert ti._try_number == 2
        assert ti.try_number == 3

        # Clear the TI state since you can't run a task with a FAILED state without
        # clearing it first
        dag.clear()

        # third run -- up for retry
        run_with_error(ti)
        assert ti.state == State.UP_FOR_RETRY
        assert ti._try_number == 3
        assert ti.try_number == 4

        # fourth run -- fail
        run_with_error(ti)
        ti.refresh_from_db()
        assert ti.state == State.FAILED
        assert ti._try_number == 4
        assert ti.try_number == 5
        assert RenderedTaskInstanceFields.get_templated_fields(ti) == expected_rendered_ti_fields

    def test_next_retry_datetime(self):
        delay = datetime.timedelta(seconds=30)
        max_delay = datetime.timedelta(minutes=60)

        dag = models.DAG(dag_id='fail_dag')
        task = BashOperator(
            task_id='task_with_exp_backoff_and_max_delay',
            bash_command='exit 1',
            retries=3,
            retry_delay=delay,
            retry_exponential_backoff=True,
            max_retry_delay=max_delay,
            dag=dag,
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=DEFAULT_DATE)
        ti.end_date = pendulum.instance(timezone.utcnow())

        date = ti.next_retry_datetime()
        # between 30 * 2^0.5 and 30 * 2^1 (15 and 30)
        period = ti.end_date.add(seconds=30) - ti.end_date.add(seconds=15)
        assert date in period

        ti.try_number = 3
        date = ti.next_retry_datetime()
        # between 30 * 2^2 and 30 * 2^3 (120 and 240)
        period = ti.end_date.add(seconds=240) - ti.end_date.add(seconds=120)
        assert date in period

        ti.try_number = 5
        date = ti.next_retry_datetime()
        # between 30 * 2^4 and 30 * 2^5 (480 and 960)
        period = ti.end_date.add(seconds=960) - ti.end_date.add(seconds=480)
        assert date in period

        ti.try_number = 9
        date = ti.next_retry_datetime()
        assert date == ti.end_date + max_delay

        ti.try_number = 50
        date = ti.next_retry_datetime()
        assert date == ti.end_date + max_delay

    def test_next_retry_datetime_short_intervals(self):
        delay = datetime.timedelta(seconds=1)
        max_delay = datetime.timedelta(minutes=60)

        dag = models.DAG(dag_id='fail_dag')
        task = BashOperator(
            task_id='task_with_exp_backoff_and_short_time_interval',
            bash_command='exit 1',
            retries=3,
            retry_delay=delay,
            retry_exponential_backoff=True,
            max_retry_delay=max_delay,
            dag=dag,
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )
        ti = TI(task=task, execution_date=DEFAULT_DATE)
        ti.end_date = pendulum.instance(timezone.utcnow())

        date = ti.next_retry_datetime()
        # between 1 * 2^0.5 and 1 * 2^1 (15 and 30)
        period = ti.end_date.add(seconds=15) - ti.end_date.add(seconds=1)
        assert date in period

    def test_reschedule_handling(self):
        """
        Test that task reschedules are handled properly
        """
        # Return values of the python sensor callable, modified during tests
        done = False
        fail = False

        def func():
            if fail:
                raise AirflowException()
            return done

        dag = models.DAG(dag_id='test_reschedule_handling')
        task = PythonSensor(
            task_id='test_reschedule_handling_sensor',
            poke_interval=0,
            mode='reschedule',
            python_callable=func,
            retries=1,
            retry_delay=datetime.timedelta(seconds=0),
            dag=dag,
            owner='airflow',
            pool='test_pool',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )

        ti = TI(task=task, execution_date=timezone.utcnow())
        assert ti._try_number == 0
        assert ti.try_number == 1

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )

        def run_ti_and_assert(
            run_date,
            expected_start_date,
            expected_end_date,
            expected_duration,
            expected_state,
            expected_try_number,
            expected_task_reschedule_count,
        ):
            with freeze_time(run_date):
                try:
                    ti.run()
                except AirflowException:
                    if not fail:
                        raise
            ti.refresh_from_db()
            assert ti.state == expected_state
            assert ti._try_number == expected_try_number
            assert ti.try_number == expected_try_number + 1
            assert ti.start_date == expected_start_date
            assert ti.end_date == expected_end_date
            assert ti.duration == expected_duration
            trs = TaskReschedule.find_for_task_instance(ti)
            assert len(trs) == expected_task_reschedule_count

        date1 = timezone.utcnow()
        date2 = date1 + datetime.timedelta(minutes=1)
        date3 = date2 + datetime.timedelta(minutes=1)
        date4 = date3 + datetime.timedelta(minutes=1)

        # Run with multiple reschedules.
        # During reschedule the try number remains the same, but each reschedule is recorded.
        # The start date is expected to remain the initial date, hence the duration increases.
        # When finished the try number is incremented and there is no reschedule expected
        # for this try.

        done, fail = False, False
        run_ti_and_assert(date1, date1, date1, 0, State.UP_FOR_RESCHEDULE, 0, 1)

        done, fail = False, False
        run_ti_and_assert(date2, date1, date2, 60, State.UP_FOR_RESCHEDULE, 0, 2)

        done, fail = False, False
        run_ti_and_assert(date3, date1, date3, 120, State.UP_FOR_RESCHEDULE, 0, 3)

        done, fail = True, False
        run_ti_and_assert(date4, date1, date4, 180, State.SUCCESS, 1, 0)

        # Clear the task instance.
        dag.clear()
        ti.refresh_from_db()
        assert ti.state == State.NONE
        assert ti._try_number == 1

        # Run again after clearing with reschedules and a retry.
        # The retry increments the try number, and for that try no reschedule is expected.
        # After the retry the start date is reset, hence the duration is also reset.

        done, fail = False, False
        run_ti_and_assert(date1, date1, date1, 0, State.UP_FOR_RESCHEDULE, 1, 1)

        done, fail = False, True
        run_ti_and_assert(date2, date1, date2, 60, State.UP_FOR_RETRY, 2, 0)

        done, fail = False, False
        run_ti_and_assert(date3, date3, date3, 0, State.UP_FOR_RESCHEDULE, 2, 1)

        done, fail = True, False
        run_ti_and_assert(date4, date3, date4, 60, State.SUCCESS, 3, 0)

    def test_reschedule_handling_clear_reschedules(self):
        """
        Test that task reschedules clearing are handled properly
        """
        # Return values of the python sensor callable, modified during tests
        done = False
        fail = False

        def func():
            if fail:
                raise AirflowException()
            return done

        dag = models.DAG(dag_id='test_reschedule_handling')
        task = PythonSensor(
            task_id='test_reschedule_handling_sensor',
            poke_interval=0,
            mode='reschedule',
            python_callable=func,
            retries=1,
            retry_delay=datetime.timedelta(seconds=0),
            dag=dag,
            owner='airflow',
            pool='test_pool',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        )

        ti = TI(task=task, execution_date=timezone.utcnow())
        assert ti._try_number == 0
        assert ti.try_number == 1

        def run_ti_and_assert(
            run_date,
            expected_start_date,
            expected_end_date,
            expected_duration,
            expected_state,
            expected_try_number,
            expected_task_reschedule_count,
        ):
            with freeze_time(run_date):
                try:
                    ti.run()
                except AirflowException:
                    if not fail:
                        raise
            ti.refresh_from_db()
            assert ti.state == expected_state
            assert ti._try_number == expected_try_number
            assert ti.try_number == expected_try_number + 1
            assert ti.start_date == expected_start_date
            assert ti.end_date == expected_end_date
            assert ti.duration == expected_duration
            trs = TaskReschedule.find_for_task_instance(ti)
            assert len(trs) == expected_task_reschedule_count

        date1 = timezone.utcnow()

        done, fail = False, False
        run_ti_and_assert(date1, date1, date1, 0, State.UP_FOR_RESCHEDULE, 0, 1)

        # Clear the task instance.
        dag.clear()
        ti.refresh_from_db()
        assert ti.state == State.NONE
        assert ti._try_number == 0
        # Check that reschedules for ti have also been cleared.
        trs = TaskReschedule.find_for_task_instance(ti)
        assert not trs

    def test_depends_on_past(self):
        dag = DAG(dag_id='test_depends_on_past', start_date=DEFAULT_DATE)

        task = DummyOperator(
            task_id='test_dop_task',
            dag=dag,
            depends_on_past=True,
        )
        dag.clear()

        dag.create_dagrun(
            execution_date=DEFAULT_DATE,
            state=State.FAILED,
            run_type=DagRunType.SCHEDULED,
        )

        run_date = task.start_date + datetime.timedelta(days=5)

        dag.create_dagrun(
            execution_date=run_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )

        ti = TI(task, run_date)

        # depends_on_past prevents the run
        task.run(start_date=run_date, end_date=run_date, ignore_first_depends_on_past=False)
        ti.refresh_from_db()
        assert ti.state is None

        # ignore first depends_on_past to allow the run
        task.run(start_date=run_date, end_date=run_date, ignore_first_depends_on_past=True)
        ti.refresh_from_db()
        assert ti.state == State.SUCCESS

    # Parameterized tests to check for the correct firing
    # of the trigger_rule under various circumstances
    # Numeric fields are in order:
    #   successes, skipped, failed, upstream_failed, done
    @parameterized.expand(
        [
            #
            # Tests for all_success
            #
            ['all_success', 5, 0, 0, 0, 0, True, None, True],
            ['all_success', 2, 0, 0, 0, 0, True, None, False],
            ['all_success', 2, 0, 1, 0, 0, True, State.UPSTREAM_FAILED, False],
            ['all_success', 2, 1, 0, 0, 0, True, State.SKIPPED, False],
            #
            # Tests for one_success
            #
            ['one_success', 5, 0, 0, 0, 5, True, None, True],
            ['one_success', 2, 0, 0, 0, 2, True, None, True],
            ['one_success', 2, 0, 1, 0, 3, True, None, True],
            ['one_success', 2, 1, 0, 0, 3, True, None, True],
            ['one_success', 0, 5, 0, 0, 5, True, State.SKIPPED, False],
            ['one_success', 0, 4, 1, 0, 5, True, State.UPSTREAM_FAILED, False],
            ['one_success', 0, 3, 1, 1, 5, True, State.UPSTREAM_FAILED, False],
            ['one_success', 0, 4, 0, 1, 5, True, State.UPSTREAM_FAILED, False],
            ['one_success', 0, 0, 5, 0, 5, True, State.UPSTREAM_FAILED, False],
            ['one_success', 0, 0, 4, 1, 5, True, State.UPSTREAM_FAILED, False],
            ['one_success', 0, 0, 0, 5, 5, True, State.UPSTREAM_FAILED, False],
            #
            # Tests for all_failed
            #
            ['all_failed', 5, 0, 0, 0, 5, True, State.SKIPPED, False],
            ['all_failed', 0, 0, 5, 0, 5, True, None, True],
            ['all_failed', 2, 0, 0, 0, 2, True, State.SKIPPED, False],
            ['all_failed', 2, 0, 1, 0, 3, True, State.SKIPPED, False],
            ['all_failed', 2, 1, 0, 0, 3, True, State.SKIPPED, False],
            #
            # Tests for one_failed
            #
            ['one_failed', 5, 0, 0, 0, 0, True, None, False],
            ['one_failed', 2, 0, 0, 0, 0, True, None, False],
            ['one_failed', 2, 0, 1, 0, 0, True, None, True],
            ['one_failed', 2, 1, 0, 0, 3, True, None, False],
            ['one_failed', 2, 3, 0, 0, 5, True, State.SKIPPED, False],
            #
            # Tests for done
            #
            ['all_done', 5, 0, 0, 0, 5, True, None, True],
            ['all_done', 2, 0, 0, 0, 2, True, None, False],
            ['all_done', 2, 0, 1, 0, 3, True, None, False],
            ['all_done', 2, 1, 0, 0, 3, True, None, False],
        ]
    )
    def test_check_task_dependencies(
        self,
        trigger_rule: str,
        successes: int,
        skipped: int,
        failed: int,
        upstream_failed: int,
        done: int,
        flag_upstream_failed: bool,
        expect_state: State,
        expect_completed: bool,
    ):
        start_date = timezone.datetime(2016, 2, 1, 0, 0, 0)
        dag = models.DAG('test-dag', start_date=start_date)
        downstream = DummyOperator(task_id='downstream', dag=dag, owner='airflow', trigger_rule=trigger_rule)
        for i in range(5):
            task = DummyOperator(task_id=f'runme_{i}', dag=dag, owner='airflow')
            task.set_downstream(downstream)
        run_date = task.start_date + datetime.timedelta(days=5)

        ti = TI(downstream, run_date)
        dep_results = TriggerRuleDep()._evaluate_trigger_rule(
            ti=ti,
            successes=successes,
            skipped=skipped,
            failed=failed,
            upstream_failed=upstream_failed,
            done=done,
            flag_upstream_failed=flag_upstream_failed,
        )
        completed = all(dep.passed for dep in dep_results)

        assert completed == expect_completed
        assert ti.state == expect_state

    def test_respects_prev_dagrun_dep(self):
        with DAG(dag_id='test_dag'):
            task = DummyOperator(task_id='task', start_date=DEFAULT_DATE)
        ti = TI(task, DEFAULT_DATE)
        failing_status = [TIDepStatus('test fail status name', False, 'test fail reason')]
        passing_status = [TIDepStatus('test pass status name', True, 'test passing reason')]
        with patch(
            'airflow.ti_deps.deps.prev_dagrun_dep.PrevDagrunDep.get_dep_statuses', return_value=failing_status
        ):
            assert not ti.are_dependencies_met()
        with patch(
            'airflow.ti_deps.deps.prev_dagrun_dep.PrevDagrunDep.get_dep_statuses', return_value=passing_status
        ):
            assert ti.are_dependencies_met()

    @parameterized.expand(
        [
            (State.SUCCESS, True),
            (State.SKIPPED, True),
            (State.RUNNING, False),
            (State.FAILED, False),
            (State.NONE, False),
        ]
    )
    def test_are_dependents_done(self, downstream_ti_state, expected_are_dependents_done):
        with DAG(dag_id='test_dag'):
            task = DummyOperator(task_id='task', start_date=DEFAULT_DATE)
            downstream_task = DummyOperator(task_id='downstream_task', start_date=DEFAULT_DATE)
            task >> downstream_task

        ti = TI(task, DEFAULT_DATE)
        downstream_ti = TI(downstream_task, DEFAULT_DATE)

        downstream_ti.set_state(downstream_ti_state)
        assert ti.are_dependents_done() == expected_are_dependents_done

    def test_xcom_pull(self):
        """
        Test xcom_pull, using different filtering methods.
        """
        dag = models.DAG(
            dag_id='test_xcom',
            schedule_interval='@monthly',
            start_date=timezone.datetime(2016, 6, 1, 0, 0, 0),
        )

        exec_date = timezone.utcnow()

        # Push a value
        task1 = DummyOperator(task_id='test_xcom_1', dag=dag, owner='airflow')
        ti1 = TI(task=task1, execution_date=exec_date)
        ti1.xcom_push(key='foo', value='bar')

        # Push another value with the same key (but by a different task)
        task2 = DummyOperator(task_id='test_xcom_2', dag=dag, owner='airflow')
        ti2 = TI(task=task2, execution_date=exec_date)
        ti2.xcom_push(key='foo', value='baz')

        # Pull with no arguments
        result = ti1.xcom_pull()
        assert result is None
        # Pull the value pushed most recently by any task.
        result = ti1.xcom_pull(key='foo')
        assert result in 'baz'
        # Pull the value pushed by the first task
        result = ti1.xcom_pull(task_ids='test_xcom_1', key='foo')
        assert result == 'bar'
        # Pull the value pushed by the second task
        result = ti1.xcom_pull(task_ids='test_xcom_2', key='foo')
        assert result == 'baz'
        # Pull the values pushed by both tasks & Verify Order of task_ids pass & values returned
        result = ti1.xcom_pull(task_ids=['test_xcom_1', 'test_xcom_2'], key='foo')
        assert result == ['bar', 'baz']

    def test_xcom_pull_after_success(self):
        """
        tests xcom set/clear relative to a task in a 'success' rerun scenario
        """
        key = 'xcom_key'
        value = 'xcom_value'

        dag = models.DAG(dag_id='test_xcom', schedule_interval='@monthly')
        task = DummyOperator(
            task_id='test_xcom',
            dag=dag,
            pool='test_xcom',
            owner='airflow',
            start_date=timezone.datetime(2016, 6, 2, 0, 0, 0),
        )
        exec_date = timezone.utcnow()
        ti = TI(task=task, execution_date=exec_date)

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )

        ti.run(mark_success=True)
        ti.xcom_push(key=key, value=value)
        assert ti.xcom_pull(task_ids='test_xcom', key=key) == value
        ti.run()
        # The second run and assert is to handle AIRFLOW-131 (don't clear on
        # prior success)
        assert ti.xcom_pull(task_ids='test_xcom', key=key) == value

        # Test AIRFLOW-703: Xcom shouldn't be cleared if the task doesn't
        # execute, even if dependencies are ignored
        ti.run(ignore_all_deps=True, mark_success=True)
        assert ti.xcom_pull(task_ids='test_xcom', key=key) == value
        # Xcom IS finally cleared once task has executed
        ti.run(ignore_all_deps=True)
        assert ti.xcom_pull(task_ids='test_xcom', key=key) is None

    def test_xcom_pull_different_execution_date(self):
        """
        tests xcom fetch behavior with different execution dates, using
        both xcom_pull with "include_prior_dates" and without
        """
        key = 'xcom_key'
        value = 'xcom_value'

        dag = models.DAG(dag_id='test_xcom', schedule_interval='@monthly')
        task = DummyOperator(
            task_id='test_xcom',
            dag=dag,
            pool='test_xcom',
            owner='airflow',
            start_date=timezone.datetime(2016, 6, 2, 0, 0, 0),
        )
        exec_date = timezone.utcnow()
        ti = TI(task=task, execution_date=exec_date)

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )

        ti.run(mark_success=True)
        ti.xcom_push(key=key, value=value)
        assert ti.xcom_pull(task_ids='test_xcom', key=key) == value
        ti.run()
        exec_date += datetime.timedelta(days=1)
        ti = TI(task=task, execution_date=exec_date)
        ti.run()
        # We have set a new execution date (and did not pass in
        # 'include_prior_dates'which means this task should now have a cleared
        # xcom value
        assert ti.xcom_pull(task_ids='test_xcom', key=key) is None
        # We *should* get a value using 'include_prior_dates'
        assert ti.xcom_pull(task_ids='test_xcom', key=key, include_prior_dates=True) == value

    def test_xcom_push_flag(self):
        """
        Tests the option for Operators to push XComs
        """
        value = 'hello'
        task_id = 'test_no_xcom_push'
        dag = models.DAG(dag_id='test_xcom')

        # nothing saved to XCom
        task = PythonOperator(
            task_id=task_id,
            dag=dag,
            python_callable=lambda: value,
            do_xcom_push=False,
            owner='airflow',
            start_date=datetime.datetime(2017, 1, 1),
        )
        ti = TI(task=task, execution_date=datetime.datetime(2017, 1, 1))
        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
        )
        ti.run()
        assert ti.xcom_pull(task_ids=task_id, key=models.XCOM_RETURN_KEY) is None

    def test_post_execute_hook(self):
        """
        Test that post_execute hook is called with the Operator's result.
        The result ('error') will cause an error to be raised and trapped.
        """

        class TestError(Exception):
            pass

        class TestOperator(PythonOperator):
            def post_execute(self, context, result=None):
                if result == 'error':
                    raise TestError('expected error.')

        dag = models.DAG(dag_id='test_post_execute_dag')
        task = TestOperator(
            task_id='test_operator',
            dag=dag,
            python_callable=lambda: 'error',
            owner='airflow',
            start_date=timezone.datetime(2017, 2, 1),
        )
        ti = TI(task=task, execution_date=timezone.utcnow())

        with pytest.raises(TestError):
            ti.run()

    def test_check_and_change_state_before_execution(self):
        dag = models.DAG(dag_id='test_check_and_change_state_before_execution')
        task = DummyOperator(task_id='task', dag=dag, start_date=DEFAULT_DATE)
        ti = TI(task=task, execution_date=timezone.utcnow())
        assert ti._try_number == 0
        assert ti.check_and_change_state_before_execution()
        # State should be running, and try_number column should be incremented
        assert ti.state == State.RUNNING
        assert ti._try_number == 1

    def test_check_and_change_state_before_execution_dep_not_met(self):
        dag = models.DAG(dag_id='test_check_and_change_state_before_execution')
        task = DummyOperator(task_id='task', dag=dag, start_date=DEFAULT_DATE)
        task2 = DummyOperator(task_id='task2', dag=dag, start_date=DEFAULT_DATE)
        task >> task2
        ti = TI(task=task2, execution_date=timezone.utcnow())
        assert not ti.check_and_change_state_before_execution()

    def test_try_number(self):
        """
        Test the try_number accessor behaves in various running states
        """
        dag = models.DAG(dag_id='test_check_and_change_state_before_execution')
        task = DummyOperator(task_id='task', dag=dag, start_date=DEFAULT_DATE)
        ti = TI(task=task, execution_date=timezone.utcnow())
        assert 1 == ti.try_number
        ti.try_number = 2
        ti.state = State.RUNNING
        assert 2 == ti.try_number
        ti.state = State.SUCCESS
        assert 3 == ti.try_number

    def test_get_num_running_task_instances(self):
        session = settings.Session()

        dag = models.DAG(dag_id='test_get_num_running_task_instances')
        dag2 = models.DAG(dag_id='test_get_num_running_task_instances_dummy')
        task = DummyOperator(task_id='task', dag=dag, start_date=DEFAULT_DATE)
        task2 = DummyOperator(task_id='task', dag=dag2, start_date=DEFAULT_DATE)

        ti1 = TI(task=task, execution_date=DEFAULT_DATE)
        ti2 = TI(task=task, execution_date=DEFAULT_DATE + datetime.timedelta(days=1))
        ti3 = TI(task=task2, execution_date=DEFAULT_DATE)
        ti1.state = State.RUNNING
        ti2.state = State.QUEUED
        ti3.state = State.RUNNING
        session.add(ti1)
        session.add(ti2)
        session.add(ti3)
        session.commit()

        assert 1 == ti1.get_num_running_task_instances(session=session)
        assert 1 == ti2.get_num_running_task_instances(session=session)
        assert 1 == ti3.get_num_running_task_instances(session=session)

    # def test_log_url(self):
    #     now = pendulum.now('Europe/Brussels')
    #     dag = DAG('dag', start_date=DEFAULT_DATE)
    #     task = DummyOperator(task_id='op', dag=dag)
    #     ti = TI(task=task, execution_date=now)
    #     d = urllib.parse.parse_qs(
    #         urllib.parse.urlparse(ti.log_url).query,
    #         keep_blank_values=True, strict_parsing=True)
    #     self.assertEqual(d['dag_id'][0], 'dag')
    #     self.assertEqual(d['task_id'][0], 'op')
    #     self.assertEqual(pendulum.parse(d['execution_date'][0]), now)

    def test_log_url(self):
        dag = DAG('dag', start_date=DEFAULT_DATE)
        task = DummyOperator(task_id='op', dag=dag)
        ti = TI(task=task, execution_date=datetime.datetime(2018, 1, 1))

        expected_url = (
            'http://localhost:8080/log?'
            'execution_date=2018-01-01T00%3A00%3A00%2B00%3A00'
            '&task_id=op'
            '&dag_id=dag'
        )
        assert ti.log_url == expected_url

    def test_mark_success_url(self):
        now = pendulum.now('Europe/Brussels')
        dag = DAG('dag', start_date=DEFAULT_DATE)
        task = DummyOperator(task_id='op', dag=dag)
        ti = TI(task=task, execution_date=now)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(ti.mark_success_url).query, keep_blank_values=True, strict_parsing=True
        )
        assert query['dag_id'][0] == 'dag'
        assert query['task_id'][0] == 'op'
        assert pendulum.parse(query['execution_date'][0]) == now

    def test_overwrite_params_with_dag_run_conf(self):
        task = DummyOperator(task_id='op')
        ti = TI(task=task, execution_date=datetime.datetime.now())
        dag_run = DagRun()
        dag_run.conf = {"override": True}
        params = {"override": False}

        ti.overwrite_params_with_dag_run_conf(params, dag_run)

        assert params["override"] is True

    def test_overwrite_params_with_dag_run_none(self):
        task = DummyOperator(task_id='op')
        ti = TI(task=task, execution_date=datetime.datetime.now())
        params = {"override": False}

        ti.overwrite_params_with_dag_run_conf(params, None)

        assert params["override"] is False

    def test_overwrite_params_with_dag_run_conf_none(self):
        task = DummyOperator(task_id='op')
        ti = TI(task=task, execution_date=datetime.datetime.now())
        params = {"override": False}
        dag_run = DagRun()

        ti.overwrite_params_with_dag_run_conf(params, dag_run)

        assert params["override"] is False

    @patch('airflow.models.taskinstance.send_email')
    def test_email_alert(self, mock_send_email):
        dag = models.DAG(dag_id='test_failure_email')
        task = BashOperator(
            task_id='test_email_alert', dag=dag, bash_command='exit 1', start_date=DEFAULT_DATE, email='to'
        )

        ti = TI(task=task, execution_date=timezone.utcnow())

        try:
            ti.run()
        except AirflowException:
            pass

        (email, title, body), _ = mock_send_email.call_args
        assert email == 'to'
        assert 'test_email_alert' in title
        assert 'test_email_alert' in body
        assert 'Try 1' in body

    @conf_vars(
        {
            ('email', 'subject_template'): '/subject/path',
            ('email', 'html_content_template'): '/html_content/path',
        }
    )
    @patch('airflow.models.taskinstance.send_email')
    def test_email_alert_with_config(self, mock_send_email):
        dag = models.DAG(dag_id='test_failure_email')
        task = BashOperator(
            task_id='test_email_alert_with_config',
            dag=dag,
            bash_command='exit 1',
            start_date=DEFAULT_DATE,
            email='to',
        )

        ti = TI(task=task, execution_date=timezone.utcnow())

        opener = mock_open(read_data='template: {{ti.task_id}}')
        with patch('airflow.models.taskinstance.open', opener, create=True):
            try:
                ti.run()
            except AirflowException:
                pass

        (email, title, body), _ = mock_send_email.call_args
        assert email == 'to'
        assert 'template: test_email_alert_with_config' == title
        assert 'template: test_email_alert_with_config' == body

    def test_set_duration(self):
        task = DummyOperator(task_id='op', email='test@test.test')
        ti = TI(
            task=task,
            execution_date=datetime.datetime.now(),
        )
        ti.start_date = datetime.datetime(2018, 10, 1, 1)
        ti.end_date = datetime.datetime(2018, 10, 1, 2)
        ti.set_duration()
        assert ti.duration == 3600

    def test_set_duration_empty_dates(self):
        task = DummyOperator(task_id='op', email='test@test.test')
        ti = TI(task=task, execution_date=datetime.datetime.now())
        ti.set_duration()
        assert ti.duration is None

    def test_success_callback_no_race_condition(self):
        callback_wrapper = CallbackWrapper()
        dag = DAG(
            'test_success_callback_no_race_condition',
            start_date=DEFAULT_DATE,
            end_date=DEFAULT_DATE + datetime.timedelta(days=10),
        )
        task = DummyOperator(
            task_id='op',
            email='test@test.test',
            on_success_callback=callback_wrapper.success_handler,
            dag=dag,
        )
        ti = TI(task=task, execution_date=datetime.datetime.now())
        ti.state = State.RUNNING
        session = settings.Session()
        session.merge(ti)

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
            session=session,
        )
        session.commit()

        callback_wrapper.wrap_task_instance(ti)
        ti._run_raw_task()
        ti._run_finished_callback()
        assert callback_wrapper.callback_ran
        assert callback_wrapper.task_state_in_callback == State.SUCCESS
        ti.refresh_from_db()
        assert ti.state == State.SUCCESS

    @staticmethod
    def _test_previous_dates_setup(
        schedule_interval: Union[str, datetime.timedelta, None], catchup: bool, scenario: List[str]
    ) -> list:
        dag_id = 'test_previous_dates'
        dag = models.DAG(dag_id=dag_id, schedule_interval=schedule_interval, catchup=catchup)
        task = DummyOperator(task_id='task', dag=dag, start_date=DEFAULT_DATE)

        def get_test_ti(session, execution_date: pendulum.DateTime, state: str) -> TI:
            dag.create_dagrun(
                run_type=DagRunType.SCHEDULED,
                state=state,
                execution_date=execution_date,
                start_date=pendulum.now('UTC'),
                session=session,
            )
            ti = TI(task=task, execution_date=execution_date)
            ti.set_state(state=State.SUCCESS, session=session)
            return ti

        with create_session() as session:  # type: Session

            date = cast(pendulum.DateTime, pendulum.parse('2019-01-01T00:00:00+00:00'))

            ret = []

            for idx, state in enumerate(scenario):
                new_date = date.add(days=idx)
                ti = get_test_ti(session, new_date, state)
                ret.append(ti)

            return ret

    _prev_dates_param_list = (
        param('cron/catchup', '0 0 * * * ', True),
        param('cron/no-catchup', '0 0 * * *', False),
        param('no-sched/catchup', None, True),
        param('no-sched/no-catchup', None, False),
        param('timedelta/catchup', datetime.timedelta(days=1), True),
        param('timedelta/no-catchup', datetime.timedelta(days=1), False),
    )

    @parameterized.expand(_prev_dates_param_list)
    def test_previous_ti(self, _, schedule_interval, catchup) -> None:

        scenario = [State.SUCCESS, State.FAILED, State.SUCCESS]

        ti_list = self._test_previous_dates_setup(schedule_interval, catchup, scenario)

        assert ti_list[0].get_previous_ti() is None

        assert ti_list[2].get_previous_ti().execution_date == ti_list[1].execution_date

        assert ti_list[2].get_previous_ti().execution_date != ti_list[0].execution_date

    @parameterized.expand(_prev_dates_param_list)
    def test_previous_ti_success(self, _, schedule_interval, catchup) -> None:

        scenario = [State.FAILED, State.SUCCESS, State.FAILED, State.SUCCESS]

        ti_list = self._test_previous_dates_setup(schedule_interval, catchup, scenario)

        assert ti_list[0].get_previous_ti(state=State.SUCCESS) is None
        assert ti_list[1].get_previous_ti(state=State.SUCCESS) is None

        assert ti_list[3].get_previous_ti(state=State.SUCCESS).execution_date == ti_list[1].execution_date

        assert ti_list[3].get_previous_ti(state=State.SUCCESS).execution_date != ti_list[2].execution_date

    @parameterized.expand(_prev_dates_param_list)
    def test_previous_execution_date_success(self, _, schedule_interval, catchup) -> None:

        scenario = [State.FAILED, State.SUCCESS, State.FAILED, State.SUCCESS]

        ti_list = self._test_previous_dates_setup(schedule_interval, catchup, scenario)

        assert ti_list[0].get_previous_execution_date(state=State.SUCCESS) is None
        assert ti_list[1].get_previous_execution_date(state=State.SUCCESS) is None
        assert ti_list[3].get_previous_execution_date(state=State.SUCCESS) == ti_list[1].execution_date
        assert ti_list[3].get_previous_execution_date(state=State.SUCCESS) != ti_list[2].execution_date

    @parameterized.expand(_prev_dates_param_list)
    def test_previous_start_date_success(self, _, schedule_interval, catchup) -> None:

        scenario = [State.FAILED, State.SUCCESS, State.FAILED, State.SUCCESS]

        ti_list = self._test_previous_dates_setup(schedule_interval, catchup, scenario)

        assert ti_list[0].get_previous_start_date(state=State.SUCCESS) is None
        assert ti_list[1].get_previous_start_date(state=State.SUCCESS) is None
        assert ti_list[3].get_previous_start_date(state=State.SUCCESS) == ti_list[1].start_date
        assert ti_list[3].get_previous_start_date(state=State.SUCCESS) != ti_list[2].start_date

    def test_get_previous_start_date_none(self):
        """
        Test that get_previous_start_date() can handle TaskInstance with no start_date.
        """
        with DAG("test_get_previous_start_date_none", start_date=DEFAULT_DATE, schedule_interval=None) as dag:
            task = DummyOperator(task_id="op")

        day_1 = DEFAULT_DATE
        day_2 = DEFAULT_DATE + datetime.timedelta(days=1)

        # Create a DagRun for day_1 and day_2. Calling ti_2.get_previous_start_date()
        # should return the start_date of ti_1 (which is None because ti_1 was not run).
        # It should not raise an error.
        dagrun_1 = dag.create_dagrun(
            execution_date=day_1,
            state=State.RUNNING,
            run_type=DagRunType.MANUAL,
        )

        dagrun_2 = dag.create_dagrun(
            execution_date=day_2,
            state=State.RUNNING,
            run_type=DagRunType.MANUAL,
        )

        ti_1 = dagrun_1.get_task_instance(task.task_id)
        ti_2 = dagrun_2.get_task_instance(task.task_id)
        ti_1.task = task
        ti_2.task = task

        assert ti_2.get_previous_start_date() == ti_1.start_date
        assert ti_1.start_date is None

    def test_pendulum_template_dates(self):
        dag = models.DAG(
            dag_id='test_pendulum_template_dates',
            schedule_interval='0 12 * * *',
            start_date=timezone.datetime(2016, 6, 1, 0, 0, 0),
        )
        task = DummyOperator(task_id='test_pendulum_template_dates_task', dag=dag)

        ti = TI(task=task, execution_date=timezone.utcnow())

        template_context = ti.get_template_context()

        assert isinstance(template_context["execution_date"], pendulum.DateTime)
        assert isinstance(template_context["next_execution_date"], pendulum.DateTime)
        assert isinstance(template_context["prev_execution_date"], pendulum.DateTime)

    @parameterized.expand(
        [
            ('{{ conn.get("a_connection").host }}', 'hostvalue'),
            ('{{ conn.get("a_connection", "unused_fallback").host }}', 'hostvalue'),
            ('{{ conn.get("missing_connection", {"host": "fallback_host"}).host }}', 'fallback_host'),
            ('{{ conn.a_connection.host }}', 'hostvalue'),
            ('{{ conn.a_connection.login }}', 'loginvalue'),
            ('{{ conn.a_connection.password }}', 'passwordvalue'),
            ('{{ conn.a_connection.extra_dejson["extra__asana__workspace"] }}', 'extra1'),
            ('{{ conn.a_connection.extra_dejson.extra__asana__workspace }}', 'extra1'),
        ]
    )
    def test_template_with_connection(self, content, expected_output):
        """
        Test the availability of variables in templates
        """
        with create_session() as session:
            clear_db_connections(add_default_connections_back=False)
            merge_conn(
                Connection(
                    conn_id="a_connection",
                    conn_type="a_type",
                    description="a_conn_description",
                    host="hostvalue",
                    login="loginvalue",
                    password="passwordvalue",
                    schema="schemavalues",
                    extra={
                        "extra__asana__workspace": "extra1",
                    },
                ),
                session,
            )

        with DAG('test-dag', start_date=DEFAULT_DATE):
            task = DummyOperator(task_id='op1')

        ti = TI(task=task, execution_date=DEFAULT_DATE)
        context = ti.get_template_context()
        result = task.render_template(content, context)
        assert result == expected_output

    @parameterized.expand(
        [
            ('{{ var.value.a_variable }}', 'a test value'),
            ('{{ var.value.get("a_variable") }}', 'a test value'),
            ('{{ var.value.get("a_variable", "unused_fallback") }}', 'a test value'),
            ('{{ var.value.get("missing_variable", "fallback") }}', 'fallback'),
        ]
    )
    def test_template_with_variable(self, content, expected_output):
        """
        Test the availability of variables in templates
        """
        Variable.set('a_variable', 'a test value')

        with DAG('test-dag', start_date=DEFAULT_DATE):
            task = DummyOperator(task_id='op1')

        ti = TI(task=task, execution_date=DEFAULT_DATE)
        context = ti.get_template_context()
        result = task.render_template(content, context)
        assert result == expected_output

    def test_template_with_variable_missing(self):
        """
        Test the availability of variables in templates
        """
        with DAG('test-dag', start_date=DEFAULT_DATE):
            task = DummyOperator(task_id='op1')

        ti = TI(task=task, execution_date=DEFAULT_DATE)
        context = ti.get_template_context()
        with pytest.raises(KeyError):
            task.render_template('{{ var.value.get("missing_variable") }}', context)

    @parameterized.expand(
        [
            ('{{ var.value.a_variable }}', '{\n  "a": {\n    "test": "value"\n  }\n}'),
            ('{{ var.json.a_variable["a"]["test"] }}', 'value'),
            ('{{ var.json.get("a_variable")["a"]["test"] }}', 'value'),
            ('{{ var.json.get("a_variable", {"a": {"test": "unused_fallback"}})["a"]["test"] }}', 'value'),
            ('{{ var.json.get("missing_variable", {"a": {"test": "fallback"}})["a"]["test"] }}', 'fallback'),
        ]
    )
    def test_template_with_json_variable(self, content, expected_output):
        """
        Test the availability of variables in templates
        """
        Variable.set('a_variable', {'a': {'test': 'value'}}, serialize_json=True)

        with DAG('test-dag', start_date=DEFAULT_DATE):
            task = DummyOperator(task_id='op1')

        ti = TI(task=task, execution_date=DEFAULT_DATE)
        context = ti.get_template_context()
        result = task.render_template(content, context)
        assert result == expected_output

    def test_template_with_json_variable_missing(self):
        with DAG('test-dag', start_date=DEFAULT_DATE):
            task = DummyOperator(task_id='op1')

        ti = TI(task=task, execution_date=DEFAULT_DATE)
        context = ti.get_template_context()
        with pytest.raises(KeyError):
            task.render_template('{{ var.json.get("missing_variable") }}', context)

    def test_execute_callback(self):
        called = False

        def on_execute_callable(context):
            nonlocal called
            called = True
            assert context['dag_run'].dag_id == 'test_dagrun_execute_callback'

        dag = DAG(
            'test_execute_callback',
            start_date=DEFAULT_DATE,
            end_date=DEFAULT_DATE + datetime.timedelta(days=10),
        )
        task = DummyOperator(
            task_id='op', email='test@test.test', on_execute_callback=on_execute_callable, dag=dag
        )
        ti = TI(task=task, execution_date=datetime.datetime.now())
        ti.state = State.RUNNING
        session = settings.Session()

        dag.create_dagrun(
            execution_date=ti.execution_date,
            state=State.RUNNING,
            run_type=DagRunType.SCHEDULED,
            session=session,
        )
        session.merge(ti)
        session.commit()

        ti._run_raw_task()
        assert called
        ti.refresh_from_db()
        assert ti.state == State.SUCCESS

    def test_handle_failure(self):
        start_date = timezone.datetime(2016, 6, 1)
        dag = models.DAG(dag_id="test_handle_failure", schedule_interval=None, start_date=start_date)

        mock_on_failure_1 = mock.MagicMock()
        mock_on_retry_1 = mock.MagicMock()
        task1 = DummyOperator(
            task_id="test_handle_failure_on_failure",
            on_failure_callback=mock_on_failure_1,
            on_retry_callback=mock_on_retry_1,
            dag=dag,
        )
        ti1 = TI(task=task1, execution_date=start_date)
        ti1.state = State.FAILED
        ti1.handle_failure("test failure handling")
        ti1._run_finished_callback()

        context_arg_1 = mock_on_failure_1.call_args[0][0]
        assert context_arg_1 and "task_instance" in context_arg_1
        mock_on_retry_1.assert_not_called()

        mock_on_failure_2 = mock.MagicMock()
        mock_on_retry_2 = mock.MagicMock()
        task2 = DummyOperator(
            task_id="test_handle_failure_on_retry",
            on_failure_callback=mock_on_failure_2,
            on_retry_callback=mock_on_retry_2,
            retries=1,
            dag=dag,
        )
        ti2 = TI(task=task2, execution_date=start_date)
        ti2.state = State.FAILED
        ti2.handle_failure("test retry handling")
        ti2._run_finished_callback()

        mock_on_failure_2.assert_not_called()

        context_arg_2 = mock_on_retry_2.call_args[0][0]
        assert context_arg_2 and "task_instance" in context_arg_2

        # test the scenario where normally we would retry but have been asked to fail
        mock_on_failure_3 = mock.MagicMock()
        mock_on_retry_3 = mock.MagicMock()
        task3 = DummyOperator(
            task_id="test_handle_failure_on_force_fail",
            on_failure_callback=mock_on_failure_3,
            on_retry_callback=mock_on_retry_3,
            retries=1,
            dag=dag,
        )
        ti3 = TI(task=task3, execution_date=start_date)
        ti3.state = State.FAILED
        ti3.handle_failure("test force_fail handling", force_fail=True)
        ti3._run_finished_callback()

        context_arg_3 = mock_on_failure_3.call_args[0][0]
        assert context_arg_3 and "task_instance" in context_arg_3
        mock_on_retry_3.assert_not_called()

    def test_does_not_retry_on_airflow_fail_exception(self):
        def fail():
            raise AirflowFailException("hopeless")

        dag = models.DAG(dag_id='test_does_not_retry_on_airflow_fail_exception')
        task = PythonOperator(
            task_id='test_raise_airflow_fail_exception',
            dag=dag,
            python_callable=fail,
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
            retries=1,
        )
        ti = TI(task=task, execution_date=timezone.utcnow())
        try:
            ti.run()
        except AirflowFailException:
            pass  # expected
        assert State.FAILED == ti.state

    def test_retries_on_other_exceptions(self):
        def fail():
            raise AirflowException("maybe this will pass?")

        dag = models.DAG(dag_id='test_retries_on_other_exceptions')
        task = PythonOperator(
            task_id='test_raise_other_exception',
            dag=dag,
            python_callable=fail,
            owner='airflow',
            start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
            retries=1,
        )
        ti = TI(task=task, execution_date=timezone.utcnow())
        try:
            ti.run()
        except AirflowException:
            pass  # expected
        assert State.UP_FOR_RETRY == ti.state

    def _env_var_check_callback(self):
        assert 'test_echo_env_variables' == os.environ['AIRFLOW_CTX_DAG_ID']
        assert 'hive_in_python_op' == os.environ['AIRFLOW_CTX_TASK_ID']
        assert DEFAULT_DATE.isoformat() == os.environ['AIRFLOW_CTX_EXECUTION_DATE']
        assert DagRun.generate_run_id(DagRunType.MANUAL, DEFAULT_DATE) == os.environ['AIRFLOW_CTX_DAG_RUN_ID']

    def test_echo_env_variables(self):
        dag = DAG(
            'test_echo_env_variables',
            start_date=DEFAULT_DATE,
            end_date=DEFAULT_DATE + datetime.timedelta(days=10),
        )
        op = PythonOperator(
            task_id='hive_in_python_op', dag=dag, python_callable=self._env_var_check_callback
        )
        dag.create_dagrun(
            run_type=DagRunType.MANUAL,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
            external_trigger=False,
        )
        ti = TI(task=op, execution_date=DEFAULT_DATE)
        ti.state = State.RUNNING
        session = settings.Session()
        session.merge(ti)
        session.commit()
        ti._run_raw_task()
        ti.refresh_from_db()
        assert ti.state == State.SUCCESS

    @patch.object(Stats, 'incr')
    def test_task_stats(self, stats_mock):
        dag = DAG(
            'test_task_start_end_stats',
            start_date=DEFAULT_DATE,
            end_date=DEFAULT_DATE + datetime.timedelta(days=10),
        )
        op = DummyOperator(task_id='dummy_op', dag=dag)
        dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
            external_trigger=False,
        )
        ti = TI(task=op, execution_date=DEFAULT_DATE)
        ti.state = State.RUNNING
        session = settings.Session()
        session.merge(ti)
        session.commit()
        ti._run_raw_task()
        ti.refresh_from_db()
        stats_mock.assert_called_with(f'ti.finish.{dag.dag_id}.{op.task_id}.{ti.state}')
        assert call(f'ti.start.{dag.dag_id}.{op.task_id}') in stats_mock.mock_calls
        assert stats_mock.call_count == 5

    def test_generate_command_default_param(self):
        dag_id = 'test_generate_command_default_param'
        task_id = 'task'
        assert_command = ['airflow', 'tasks', 'run', dag_id, task_id, DEFAULT_DATE.isoformat()]
        generate_command = TI.generate_command(dag_id=dag_id, task_id=task_id, execution_date=DEFAULT_DATE)
        assert assert_command == generate_command

    def test_generate_command_specific_param(self):
        dag_id = 'test_generate_command_specific_param'
        task_id = 'task'
        assert_command = [
            'airflow',
            'tasks',
            'run',
            dag_id,
            task_id,
            DEFAULT_DATE.isoformat(),
            '--mark-success',
        ]
        generate_command = TI.generate_command(
            dag_id=dag_id, task_id=task_id, execution_date=DEFAULT_DATE, mark_success=True
        )
        assert assert_command == generate_command

    def test_get_rendered_template_fields(self):

        with DAG('test-dag', start_date=DEFAULT_DATE):
            task = BashOperator(task_id='op1', bash_command="{{ task.task_id }}")

        ti = TI(task=task, execution_date=DEFAULT_DATE)

        with create_session() as session:
            session.add(RenderedTaskInstanceFields(ti))

        # Create new TI for the same Task
        with DAG('test-dag', start_date=DEFAULT_DATE):
            new_task = BashOperator(task_id='op1', bash_command="{{ task.task_id }}")

        new_ti = TI(task=new_task, execution_date=DEFAULT_DATE)
        new_ti.get_rendered_template_fields()

        assert "op1" == ti.task.bash_command

        # CleanUp
        with create_session() as session:
            session.query(RenderedTaskInstanceFields).delete()

    @mock.patch.dict(os.environ, {"AIRFLOW_IS_K8S_EXECUTOR_POD": "True"})
    @mock.patch("airflow.settings.pod_mutation_hook")
    def test_render_k8s_pod_yaml(self, pod_mutation_hook):
        with DAG('test_get_rendered_k8s_spec', start_date=DEFAULT_DATE):
            task = BashOperator(task_id='op1', bash_command="{{ task.task_id }}")

        ti = TI(task=task, execution_date=DEFAULT_DATE)

        expected_pod_spec = {
            'metadata': {
                'annotations': {
                    'dag_id': 'test_get_rendered_k8s_spec',
                    'execution_date': '2016-01-01T00:00:00+00:00',
                    'task_id': 'op1',
                    'try_number': '1',
                },
                'labels': {
                    'airflow-worker': 'worker-config',
                    'airflow_version': version,
                    'dag_id': 'test_get_rendered_k8s_spec',
                    'execution_date': '2016-01-01T00_00_00_plus_00_00',
                    'kubernetes_executor': 'True',
                    'task_id': 'op1',
                    'try_number': '1',
                },
                'name': mock.ANY,
                'namespace': 'default',
            },
            'spec': {
                'containers': [
                    {
                        'args': [
                            'airflow',
                            'tasks',
                            'run',
                            'test_get_rendered_k8s_spec',
                            'op1',
                            '2016-01-01T00:00:00+00:00',
                        ],
                        'image': ':',
                        'name': 'base',
                        'env': [{'name': 'AIRFLOW_IS_K8S_EXECUTOR_POD', 'value': 'True'}],
                    }
                ]
            },
        }

        assert ti.render_k8s_pod_yaml() == expected_pod_spec
        pod_mutation_hook.assert_called_once_with(mock.ANY)

    @mock.patch.dict(os.environ, {"AIRFLOW_IS_K8S_EXECUTOR_POD": "True"})
    @mock.patch.object(RenderedTaskInstanceFields, 'get_k8s_pod_yaml')
    def test_get_rendered_k8s_spec(self, rtif_get_k8s_pod_yaml):
        # Create new TI for the same Task
        with DAG('test_get_rendered_k8s_spec', start_date=DEFAULT_DATE):
            task = BashOperator(task_id='op1', bash_command="{{ task.task_id }}")

        ti = TI(task=task, execution_date=DEFAULT_DATE)

        patcher = mock.patch.object(ti, 'render_k8s_pod_yaml', autospec=True)

        fake_spec = {"ermagawds": "pods"}

        session = mock.Mock()

        with patcher as render_k8s_pod_yaml:
            rtif_get_k8s_pod_yaml.return_value = fake_spec
            assert ti.get_rendered_k8s_spec(session) == fake_spec

            rtif_get_k8s_pod_yaml.assert_called_once_with(ti, session=session)
            render_k8s_pod_yaml.assert_not_called()

            # Now test that when we _dont_ find it in the DB, it calles render_k8s_pod_yaml
            rtif_get_k8s_pod_yaml.return_value = None
            render_k8s_pod_yaml.return_value = fake_spec

            assert ti.get_rendered_k8s_spec(session) == fake_spec

            render_k8s_pod_yaml.assert_called_once()

    def test_set_state_up_for_retry(self):
        dag = DAG('dag', start_date=DEFAULT_DATE)
        op1 = DummyOperator(task_id='op_1', owner='test', dag=dag)

        ti = TI(task=op1, execution_date=timezone.utcnow(), state=State.RUNNING)
        start_date = timezone.utcnow()
        ti.start_date = start_date

        ti.set_state(State.UP_FOR_RETRY)
        assert ti.state == State.UP_FOR_RETRY
        assert ti.start_date == start_date, "Start date should have been left alone"
        assert ti.start_date < ti.end_date
        assert ti.duration > 0

    def test_refresh_from_db(self):
        run_date = timezone.utcnow()

        expected_values = {
            "task_id": "test_refresh_from_db_task",
            "dag_id": "test_refresh_from_db_dag",
            "execution_date": run_date,
            "start_date": run_date + datetime.timedelta(days=1),
            "end_date": run_date + datetime.timedelta(days=1, seconds=1, milliseconds=234),
            "duration": 1.234,
            "state": State.SUCCESS,
            "_try_number": 1,
            "max_tries": 1,
            "hostname": "some_unique_hostname",
            "unixname": "some_unique_unixname",
            "job_id": 1234,
            "pool": "some_fake_pool_id",
            "pool_slots": 25,
            "queue": "some_queue_id",
            "priority_weight": 123,
            "operator": "some_custom_operator",
            "queued_dttm": run_date + datetime.timedelta(hours=1),
            "queued_by_job_id": 321,
            "pid": 123,
            "executor_config": {"Some": {"extra": "information"}},
            "external_executor_id": "some_executor_id",
        }
        # Make sure we aren't missing any new value in our expected_values list.
        expected_keys = {f"task_instance.{key.lstrip('_')}" for key in expected_values.keys()}
        assert {str(c) for c in TI.__table__.columns} == expected_keys, (
            "Please add all non-foreign values of TaskInstance to this list. "
            "This prevents refresh_from_db() from missing a field."
        )

        operator = DummyOperator(task_id=expected_values['task_id'])
        ti = TI(task=operator, execution_date=expected_values['execution_date'])
        for key, expected_value in expected_values.items():
            setattr(ti, key, expected_value)
        with create_session() as session:
            session.merge(ti)
            session.commit()

        mock_task = mock.MagicMock()
        mock_task.task_id = expected_values["task_id"]
        mock_task.dag_id = expected_values["dag_id"]

        ti = TI(task=mock_task, execution_date=run_date)
        ti.refresh_from_db()
        for key, expected_value in expected_values.items():
            assert hasattr(ti, key), f"Key {key} is missing in the TaskInstance."
            assert (
                getattr(ti, key) == expected_value
            ), f"Key: {key} had different values. Make sure it loads it in the refresh refresh_from_db()"


@pytest.mark.parametrize("pool_override", [None, "test_pool2"])
def test_refresh_from_task(pool_override):
    task = DummyOperator(
        task_id="dummy",
        queue="test_queue",
        pool="test_pool1",
        pool_slots=3,
        priority_weight=10,
        run_as_user="test",
        retries=30,
        executor_config={"KubernetesExecutor": {"image": "myCustomDockerImage"}},
    )
    ti = TI(task, execution_date=pendulum.datetime(2020, 1, 1))
    ti.refresh_from_task(task, pool_override=pool_override)

    assert ti.queue == task.queue

    if pool_override:
        assert ti.pool == pool_override
    else:
        assert ti.pool == task.pool

    assert ti.pool_slots == task.pool_slots
    assert ti.priority_weight == task.priority_weight_total
    assert ti.run_as_user == task.run_as_user
    assert ti.max_tries == task.retries
    assert ti.executor_config == task.executor_config
    assert ti.operator == DummyOperator.__name__


class TestRunRawTaskQueriesCount(unittest.TestCase):
    """
    These tests are designed to detect changes in the number of queries executed
    when calling _run_raw_task
    """

    @staticmethod
    def _clean():
        db.clear_db_runs()
        db.clear_db_pools()
        db.clear_db_dags()
        db.clear_db_sla_miss()
        db.clear_db_import_errors()

    def setUp(self) -> None:
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    @parameterized.expand(
        [
            # Expected queries, mark_success
            (12, False),
            (7, True),
        ]
    )
    def test_execute_queries_count(self, expected_query_count, mark_success):
        with create_session() as session:
            dag = DAG('test_queries', start_date=DEFAULT_DATE)
            task = DummyOperator(task_id='op', dag=dag)
            ti = TI(task=task, execution_date=datetime.datetime.now())
            ti.state = State.RUNNING

            session.merge(ti)
            dag.create_dagrun(
                execution_date=ti.execution_date,
                state=State.RUNNING,
                run_type=DagRunType.SCHEDULED,
                session=session,
            )
        # an extra query is fired in RenderedTaskInstanceFields.delete_old_records
        # for other DBs. delete_old_records is called only when mark_success is False
        expected_query_count_based_on_db = (
            expected_query_count + 1
            if session.bind.dialect.name == "mssql" and expected_query_count > 0 and not mark_success
            else expected_query_count
        )

        with assert_queries_count(expected_query_count_based_on_db):
            ti._run_raw_task(mark_success=mark_success)

    def test_execute_queries_count_store_serialized(self):
        with create_session() as session:
            dag = DAG('test_queries', start_date=DEFAULT_DATE)
            task = DummyOperator(task_id='op', dag=dag)
            ti = TI(task=task, execution_date=datetime.datetime.now())
            ti.state = State.RUNNING

            session.merge(ti)
            dag.create_dagrun(
                execution_date=ti.execution_date,
                state=State.RUNNING,
                run_type=DagRunType.SCHEDULED,
                session=session,
            )
        # an extra query is fired in RenderedTaskInstanceFields.delete_old_records
        # for other DBs
        expected_query_count_based_on_db = 13 if session.bind.dialect.name == "mssql" else 12

        with assert_queries_count(expected_query_count_based_on_db):
            ti._run_raw_task()

    def test_operator_field_with_serialization(self):

        dag = DAG('test_queries', start_date=DEFAULT_DATE)
        task = DummyOperator(task_id='op', dag=dag)
        assert task.task_type == 'DummyOperator'

        # Verify that ti.operator field renders correctly "without" Serialization
        ti = TI(task=task, execution_date=datetime.datetime.now())
        assert ti.operator == "DummyOperator"

        serialized_op = SerializedBaseOperator.serialize_operator(task)
        deserialized_op = SerializedBaseOperator.deserialize_operator(serialized_op)
        assert deserialized_op.task_type == 'DummyOperator'
        # Verify that ti.operator field renders correctly "with" Serialization
        ser_ti = TI(task=deserialized_op, execution_date=datetime.datetime.now())
        assert ser_ti.operator == "DummyOperator"


@pytest.mark.parametrize("mode", ["poke", "reschedule"])
@pytest.mark.parametrize("retries", [0, 1])
def test_sensor_timeout(mode, retries):
    """
    Test that AirflowSensorTimeout does not cause sensor to retry.
    """

    def timeout():
        raise AirflowSensorTimeout

    dag = models.DAG(dag_id=f'test_sensor_timeout_{mode}_{retries}')
    mock_on_failure = mock.MagicMock()
    task = PythonSensor(
        task_id='test_raise_sensor_timeout',
        dag=dag,
        python_callable=timeout,
        owner='airflow',
        start_date=timezone.datetime(2016, 2, 1, 0, 0, 0),
        on_failure_callback=mock_on_failure,
        retries=retries,
        mode=mode,
    )
    ti = TI(task=task, execution_date=timezone.utcnow())

    with pytest.raises(AirflowSensorTimeout):
        ti.run()

    assert mock_on_failure.called
    assert ti.state == State.FAILED
